"""Đường điều phối TRƯỚC-GitOps dùng chung, và lệnh `deploy-check`.

Đây là MỘT bộ mã cho hai người gọi:

  * `idpctl pre-gitops`  — bước gộp mà `.github/workflows/deploy.yaml` chạy trước khi làm
    GitOps (commit config repo, Fleet, verify namespace chính thức). Nó thay cho chuỗi
    step preflight → render → vault-auto-setup → apply-secrets rời rạc trước đây.

  * `idpctl deploy-check` — chạy CHÍNH đường điều phối đó từ máy lập trình viên, rồi thử
    triển khai thật vào một namespace kiểm tra riêng theo run-id và dọn sạch sau đó, để
    bắt lỗi trước khi push và chờ GitHub Actions.

Vì cả hai đi qua `run_pre_gitops`, một lỗi ở tầng render/Vault/secret/dry-run được phát
hiện y hệt nhau ở CI lẫn ở local — không có hai bộ logic để lệch nhau.

Chẩn đoán ở đây luôn nói RÕ TẦNG lỗi (SOURCE/PLATFORM/GITHUB/VAULT/DATABASE/KUBERNETES/
NETWORK) và chỗ phải sửa, thay vì ném ra một exception kỹ thuật trần trụi — vì cả điểm của
lệnh này là trả lời "sửa ở đâu", không phải "hỏng ở dòng nào".
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field

from . import context as _context
from . import resources as _resources
from . import render as _render
from . import delivery as _delivery
from . import audit as _audit
for _module in (_context, _resources, _render, _delivery):
    globals().update({n: getattr(_module, n) for n in dir(_module) if not n.startswith("__")})


# --------------------------------------------------------------------------------------
# Tầng lỗi + chẩn đoán
# --------------------------------------------------------------------------------------
# Bảy tầng, khớp đúng yêu cầu chẩn đoán. Chúng KHÔNG chồng lấn: một lỗi được xếp vào tầng
# mà người sửa phải mở ra — score.yaml/values là SOURCE, gateway/storageclass là PLATFORM
# (sai toạ độ) hoặc KUBERNETES (cụm thiếu thật), token/repo là GITHUB, v.v.
LAYER_SOURCE = "SOURCE"
LAYER_PLATFORM = "PLATFORM"
LAYER_GITHUB = "GITHUB"
LAYER_VAULT = "VAULT"
LAYER_DATABASE = "DATABASE"
LAYER_KUBERNETES = "KUBERNETES"
LAYER_NETWORK = "NETWORK"
LAYERS = (LAYER_SOURCE, LAYER_PLATFORM, LAYER_GITHUB, LAYER_VAULT,
          LAYER_DATABASE, LAYER_KUBERNETES, LAYER_NETWORK)


class DeployCheckError(Exception):
    """Một bước của đường điều phối hỏng, kèm đủ thứ để in chẩn đoán phân tầng.

    KHÔNG kế thừa SystemExit: người gọi (`cmd_pre_gitops`, `cmd_deploy_check`) phải BẮT
    được nó để còn kịp dọn tài nguyên tạm rồi mới thoát khác 0. Một SystemExit trần sẽ
    nhảy qua khối dọn dẹp.
    """

    def __init__(self, stage: str, cause: str, layer: str, fix_at: str,
                 next_check: str = ""):
        if layer not in LAYERS:
            raise ValueError(f"tầng lỗi không hợp lệ: {layer!r}")
        self.stage = stage
        self.cause = cause
        self.layer = layer
        self.fix_at = fix_at
        self.next_check = next_check
        super().__init__(f"[{layer}] {stage}: {cause}")


def format_diagnostic(err: DeployCheckError, *, run_id: str = "",
                      cleanup: str = "") -> str:
    """Khối chẩn đoán theo đúng hình dạng hợp đồng.

    `run_id`/`cleanup` chỉ có nghĩa với deploy-check (nó tạo tài nguyên tạm). pre-gitops
    trong CI không tạo namespace kiểm tra nên bỏ trống hai dòng đó.
    """
    lines = [
        f"[FAIL] {err.stage}",
        f"Nguyên nhân: {err.cause}",
        f"Tầng lỗi: {err.layer}",
        f"Sửa tại: {err.fix_at}",
        f"Kiểm tra tiếp: {err.next_check or '(không có gợi ý)'}",
    ]
    if run_id:
        lines.append(f"Run ID: {run_id}")
    if cleanup:
        lines.append(f"Cleanup: {cleanup}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Tham số + kết quả của đường điều phối dùng chung
# --------------------------------------------------------------------------------------
@dataclass
class PipelineParams:
    app: str
    env: str
    app_dir: Path
    sha: str
    registry: str
    work: Path
    out: Path                       # nơi ghi manifest công khai (config repo, hoặc work/)
    catalog: Path
    image: str = ""
    tag_strategy: str = ""
    # Nhãn ảnh dùng khi render/plan. Rỗng = dùng `sha`. deploy-check --build đặt run-id vào
    # đây để manifest trỏ ảnh TẠM vừa build, thay vì ảnh theo SHA (chưa chứa thay đổi chưa
    # commit). `sha` vẫn là định danh cho audit/đối chiếu, không đổi.
    render_tag: str = ""
    kubeconfig: str | None = None
    target_namespace: str = ""      # namespace mà vault/secrets/dry-run nhắm vào
    # Trạng thái score-k8s: production giữ trong Secret của cụm; check dùng file tạm để
    # KHÔNG chạm vào state thật (nó chứa GUID + mật khẩu database của bản chạy thật).
    state_file: str | None = None
    no_state: bool = False
    accept_empty_database: bool = False
    # Credential registry cho imagePullSecret. Rỗng = bỏ qua (apply-secrets tự cảnh báo).
    harbor_host: str = ""
    harbor_user: str = ""
    harbor_pass: str = ""
    backup_key_id: str = ""
    backup_secret_key: str = ""
    # Bật/tắt từng nửa tác dụng-phụ để deploy-check tái dùng đúng logic mà không đụng
    # tài nguyên thật của app.
    do_vault: bool = True
    do_secrets: bool = True
    do_dry_run: bool = True
    require_cluster: bool = True


@dataclass
class PipelineResult:
    manifests: Path                 # manifest công khai đã render
    secrets: Path                   # secrets sinh ra (cluster-only), có thể không tồn tại
    services: list = field(default_factory=list)
    catalog_ref: str = ""
    image_plan: dict = field(default_factory=dict)
    target_namespace: str = ""


def _null_record(*_a, **_k) -> None:
    pass


def read_platform_lock(app_dir: Path) -> str:
    """Ref catalog mà app ghim, đọc từ app_dir/platform.lock.

    Bỏ comment và khoảng trắng; rỗng/không có file = 'main' (giống bước Resolve catalog
    version trong deploy.yaml). Đây là NGUỒN quyết định catalog nào được render — cùng quy
    tắc mà workflow đang dùng, đặt vào một chỗ để hai bên không lệch.
    """
    lock = Path(app_dir) / "platform.lock"
    if not lock.is_file():
        return "main"
    for line in lock.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return "main"


# --------------------------------------------------------------------------------------
# Đường điều phối dùng chung — 12 bước TRƯỚC GitOps
# --------------------------------------------------------------------------------------
def run_pre_gitops(p: PipelineParams, *, record=None) -> PipelineResult:
    """Kiểm tra công cụ → cụm → source → SHA → lock → catalog → config → capability →
    render → Vault → secret → dry-run. Trả về artefact để bước GitOps (hoặc bước thử
    ephemeral của deploy-check) dùng tiếp.

    `record(stage, status, message=None, ...)` là hook để người gọi ghi audit theo TỪNG
    stage — nhờ vậy gộp bốn step rời của deploy.yaml thành một lệnh mà KHÔNG mất chi tiết
    audit từng chặng. Mọi lỗi được dịch sang DeployCheckError có tầng, không rò ra
    SystemExit trần.
    """
    record = record or _null_record
    target_ns = p.target_namespace or app_namespace(p.app, p.env)

    # 1. công cụ ------------------------------------------------------------------------
    record("preflight", "start")
    missing = [t for t in ("score-k8s", "kubectl", "git", "gh") if not shutil.which(t)]
    if missing:
        raise DeployCheckError(
            "kiểm tra công cụ", f"thiếu công cụ trên máy: {', '.join(missing)}",
            LAYER_PLATFORM, "cài đặt runner/máy lập trình viên (PATH)",
            "which score-k8s kubectl git gh")
    try:
        check_tool_versions(["score-k8s"], force=True)
    except SystemExit as exc:
        raise DeployCheckError(
            "ghim phiên bản công cụ", str(exc), LAYER_PLATFORM,
            "ci.score_k8s_version trong platform.env.yaml, hoặc phiên bản score-k8s trên máy",
            "score-k8s --version") from None

    # 2. kết nối cluster ----------------------------------------------------------------
    if p.require_cluster:
        cp = kubectl(["version", "--output=json"], kubeconfig=p.kubeconfig,
                     check=False, capture=True)
        if cp.returncode != 0:
            raise DeployCheckError(
                "kết nối cluster", f"cụm không truy cập được: {(cp.stderr or '').strip()[:200]}",
                LAYER_NETWORK, "--kubeconfig, hoặc VPN/mạng tới API server",
                "kubectl --kubeconfig <path> version")
        log("cụm truy cập được")

    # 3. source ứng dụng ----------------------------------------------------------------
    if not Path(p.app_dir).is_dir():
        raise DeployCheckError(
            "kiểm tra source", f"--app-dir không tồn tại: {p.app_dir}",
            LAYER_SOURCE, "--app-dir", "ls <app-dir>")
    try:
        services = discover(Path(p.app_dir))
    except SystemExit as exc:
        raise DeployCheckError(
            "khám phá score file", str(exc), LAYER_SOURCE,
            "score.yaml của app (không tìm thấy hoặc thiếu metadata.name/containers)",
            "ls <app-dir>/score.yaml") from None
    record("preflight", "success")

    # 4. xác nhận commit SHA (bước riêng của deploy-check, xem cmd_deploy_check) ---------
    # 5. đọc platform.lock --------------------------------------------------------------
    record("checkout", "start")
    catalog_ref = read_platform_lock(Path(p.app_dir))
    log(f"catalog ghim theo platform.lock: {catalog_ref}")

    # 6. xác định catalog được ghim -----------------------------------------------------
    catalog = Path(p.catalog)
    if not (catalog / "provisioners").is_dir():
        raise DeployCheckError(
            "định vị catalog", f"không thấy provisioners/ trong catalog {catalog}",
            LAYER_PLATFORM, "--catalog (checkout kho platform tại ref của platform.lock)",
            f"ls {catalog}/provisioners")

    # 7. đọc platform.env.yaml ----------------------------------------------------------
    # Đã nạp vào CONFIG qua --env-config; xác nhận có toạ độ tối thiểu để render.
    try:
        CONFIG.require("registry.path")
    except SystemExit as exc:
        raise DeployCheckError(
            "đọc platform.env.yaml", str(exc), LAYER_PLATFORM,
            "platform.env.yaml (--env-config)", "idpctl config --get registry.path") from None
    record("checkout", "success")

    # 8. capability của cluster ---------------------------------------------------------
    if p.require_cluster:
        def probe(pargs):
            r = kubectl(pargs, kubeconfig=p.kubeconfig, check=False, capture=True)
            return r.returncode, (r.stdout or ""), (r.stderr or "")
        blockers = [r for r in run_doctor_checks(probe) if r["level"] == DOCTOR_FAIL]
        if blockers:
            first = blockers[0]
            raise DeployCheckError(
                "kiểm capability cụm", "; ".join(b["message"] for b in blockers),
                _doctor_layer(first["capability"]),
                f"{first['config_key']} trong platform.env.yaml, hoặc cài thiếu trên cụm",
                "idpctl doctor")

    # 9. render manifest ----------------------------------------------------------------
    record("render", "start")
    render_tag = p.render_tag or p.sha
    render_args = argparse.Namespace(
        app=p.app, image=p.image or p.app, tag=render_tag, registry=p.registry,
        tag_strategy=p.tag_strategy, env=p.env, out=str(p.out),
        catalog=str(catalog), app_dir=str(p.app_dir), work=str(p.work),
        kubeconfig=p.kubeconfig, accept_empty_database=p.accept_empty_database,
        state_file=p.state_file, no_state=p.no_state)
    try:
        cmd_render(render_args)
    except SystemExit as exc:
        raise DeployCheckError(
            "render manifest", str(exc), _render_layer(str(exc)),
            "score.yaml / .score-values / provisioner / platform.env.yaml (theo nguyên nhân)",
            "idpctl render ... (đọc thông báo ở trên)") from None
    secrets_path = Path(p.work) / "secrets.yaml"
    image_plan = plan_images(services, p.registry, p.image or p.app, render_tag,
                             Path(p.app_dir),
                             resolve_tag_strategy(Path(p.app_dir), p.tag_strategy))
    record("render", "success")

    # 10. cấu hình Vault ----------------------------------------------------------------
    if p.do_vault:
        try:
            va = argparse.Namespace(app=p.app, env=p.env, apply=True,
                                    kubeconfig=p.kubeconfig)
            cmd_vault_auto_setup(va)
        except SystemExit as exc:
            raise DeployCheckError(
                "cấu hình Vault", str(exc), LAYER_VAULT,
                "policy/role Vault, VaultConnection/VaultAuthGlobal, hoặc vault.* trong config",
                "idpctl preflight --require-cluster --require-vault") from None

    # 11. chuẩn bị secret Kubernetes ----------------------------------------------------
    record("apply_secrets", "start")
    if p.do_secrets:
        try:
            sa = argparse.Namespace(
                app=p.app, env=p.env, secrets=str(secrets_path),
                harbor_host=p.harbor_host, harbor_user=p.harbor_user,
                harbor_pass=p.harbor_pass, backup_key_id=p.backup_key_id or None,
                backup_secret_key=p.backup_secret_key or None, kubeconfig=p.kubeconfig)
            # apply-secrets tự dùng app_namespace(); trong chế độ check ta ghi đè bằng cách
            # tạm đổi namespace_pattern là không sạch, nên deploy-check tự apply secret vào
            # ephemeral ns riêng và đặt do_secrets=False. Ở đây (production) target là ns
            # chính thức, đúng như deploy.yaml cũ.
            cmd_apply_secrets(sa)
        except SystemExit as exc:
            raise DeployCheckError(
                "chuẩn bị secret Kubernetes", str(exc), LAYER_KUBERNETES,
                "quyền tạo Secret/namespace trên cụm, hoặc credential registry",
                f"kubectl get ns {target_ns}") from None
    record("apply_secrets", "success")

    # 12. Kubernetes server-side dry-run ------------------------------------------------
    if p.do_dry_run:
        record("dry_run", "start")
        server_side_dry_run(p.out, target_ns, p.kubeconfig)
        record("dry_run", "success")

    return PipelineResult(
        manifests=Path(p.out), secrets=secrets_path, services=services,
        catalog_ref=catalog_ref, image_plan=image_plan, target_namespace=target_ns)


def server_side_dry_run(manifests: Path, ns: str, kubeconfig: str | None) -> None:
    """`kubectl apply --dry-run=server` cho manifest công khai vừa render.

    Đây là bước MỚI so với deploy.yaml cũ: nó bắt lỗi mà render một mình không thấy — CRD
    thiếu, field sai schema, admission webhook từ chối, namespace không tồn tại. Dùng
    dry-run=server (không phải client) vì chỉ server mới chạy validation + admission thật.
    """
    if not Path(manifests).is_file():
        raise DeployCheckError(
            "dry-run", f"không có manifest để dry-run: {manifests}",
            LAYER_PLATFORM, "bước render", "ls " + str(manifests))
    cp = kubectl(["apply", "--dry-run=server", "-n", ns, "-f", str(manifests)],
                 kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode != 0:
        err = ((cp.stderr or "") + (cp.stdout or "")).strip()
        raise DeployCheckError(
            "dry-run server-side", err[:400], _dry_run_layer(err),
            "manifest render (CRD thiếu / sai schema / admission từ chối)",
            f"kubectl apply --dry-run=server -n {ns} -f {manifests}")
    log(f"dry-run server-side đạt trong namespace {ns}")


def _doctor_layer(capability: str) -> str:
    if capability.startswith("database"):
        return LAYER_DATABASE
    if capability.startswith("vault"):
        return LAYER_VAULT
    if capability.startswith("gateway"):
        return LAYER_NETWORK
    return LAYER_KUBERNETES


def _render_layer(msg: str) -> str:
    """Phân tầng lỗi render: score/app -> SOURCE, còn lại -> PLATFORM."""
    low = msg.lower()
    if any(s in low for s in ("score", "container", "metadata.name", "no score",
                              "type: environment", ".score-values", "secretref")):
        return LAYER_SOURCE
    return LAYER_PLATFORM


def _dry_run_layer(msg: str) -> str:
    low = msg.lower()
    if "not found" in low and "namespace" in low:
        return LAYER_KUBERNETES
    if "no matches for kind" in low or "could not find the requested resource" in low:
        return LAYER_KUBERNETES
    return LAYER_KUBERNETES


# --------------------------------------------------------------------------------------
# Kiểm tra GitHub — CHỈ những gì local xác minh được
# --------------------------------------------------------------------------------------
def _gh_repo_exists(slug: str) -> bool | None:
    """slug=org/repo. True/False, None nếu không hỏi được (không mạng / chưa đăng nhập)."""
    cp = run(["gh", "api", f"repos/{slug}", "--jq", ".full_name"],
             check=False, capture=True)
    if cp.returncode == 0:
        return bool((cp.stdout or "").strip())
    err = (cp.stderr or "").lower()
    if "not found" in err or "404" in err:
        return False
    return None


def _gh_branch_exists(slug: str, branch: str) -> bool | None:
    cp = run(["gh", "api", f"repos/{slug}/branches/{branch}", "--jq", ".name"],
             check=False, capture=True)
    if cp.returncode == 0:
        return bool((cp.stdout or "").strip())
    err = (cp.stderr or "").lower()
    if "not found" in err or "404" in err:
        return False
    return None


def _gh_commit_exists(slug: str, sha: str) -> bool | None:
    cp = run(["gh", "api", f"repos/{slug}/commits/{sha}", "--jq", ".sha"],
             check=False, capture=True)
    if cp.returncode == 0:
        return bool((cp.stdout or "").strip())
    err = (cp.stderr or "").lower()
    if "not found" in err or "404" in err or "422" in err:
        return False
    return None


def github_checks(app: str, env: str, sha: str, *, check_pushed: bool = True) -> None:
    """Xác minh những gì máy local KIỂM ĐƯỢC về GitHub. Không có -> DeployCheckError.

    Cố ý KHÔNG khẳng định thứ chỉ GitHub Actions đọc được (giá trị Actions Secrets như
    KUBECONFIG_*, VAULT_TOKEN…). Chúng được BÁO RÕ là không xác minh từ local — im lặng
    coi là hợp lệ chính là cái bẫy khiến deploy hỏng ở CI dù local báo xanh.
    """
    # gh đã đăng nhập chưa
    cp = run(["gh", "auth", "status"], check=False, capture=True)
    if cp.returncode != 0:
        raise DeployCheckError(
            "GitHub đăng nhập", "GitHub CLI chưa đăng nhập", LAYER_GITHUB,
            "chạy `gh auth login`", "gh auth status")

    org = CONFIG.get("git.org") or ""
    app_pattern = CONFIG.get("git.app_repo_pattern") or "{app}"
    cfg_pattern = CONFIG.get("git.config_repo_pattern") or "{app}-config"
    app_repo = f"{org}/{app_pattern.replace('{app}', app)}" if org else \
        app_pattern.replace("{app}", app)
    cfg_repo = f"{org}/{cfg_pattern.replace('{app}', app)}" if org else \
        cfg_pattern.replace("{app}", app)

    # kho ứng dụng tồn tại + đọc được
    exists = _gh_repo_exists(app_repo)
    if exists is False:
        raise DeployCheckError(
            "kho ứng dụng", f"không thấy hoặc không có quyền đọc {app_repo}",
            LAYER_GITHUB, "git.org / git.app_repo_pattern trong platform.env.yaml, hoặc quyền repo",
            f"gh api repos/{app_repo}")
    if exists is None:
        warn(f"không hỏi được kho {app_repo} (mạng/đăng nhập) — bỏ qua kiểm này")

    # SHA có trên GitHub chưa (chỉ khi đang kiểm một commit đã push)
    if check_pushed and exists:
        got = _gh_commit_exists(app_repo, sha)
        if got is False:
            raise DeployCheckError(
                "commit trên GitHub", f"commit {sha[:12]} chưa có trên {app_repo}",
                LAYER_GITHUB, "push commit lên GitHub, hoặc dùng --build cho source chưa push",
                f"gh api repos/{app_repo}/commits/{sha}")

    # kho cấu hình + nhánh của môi trường
    cfg_exists = _gh_repo_exists(cfg_repo)
    if cfg_exists is False:
        # Chưa có kho cấu hình KHÔNG phải lỗi cứng: deploy.yaml tự tạo ở lần đầu. Cảnh báo.
        warn(f"kho cấu hình {cfg_repo} chưa tồn tại — deploy.yaml sẽ tạo ở lần chạy đầu")
    elif cfg_exists:
        branch = CONFIG.get(f"environments.{env}.config_branch") \
            or CONFIG.get("git.default_branch") or "main"
        has_branch = _gh_branch_exists(cfg_repo, branch)
        if has_branch is False:
            warn(f"kho cấu hình {cfg_repo} chưa có nhánh {branch} — deploy.yaml sẽ tạo")

    log("kiểm GitHub (local xác minh được) đạt")
    warn("KHÔNG xác minh được từ local: giá trị Actions Secrets (KUBECONFIG_*, VAULT_TOKEN, "
         "REGISTRY_*). Chỉ GitHub Actions đọc được chúng — coi là 'chưa xác minh', không "
         "phải 'hợp lệ'.")


# --------------------------------------------------------------------------------------
# deploy-check — tài nguyên tạm theo run-id + dọn dẹp bắt buộc
# --------------------------------------------------------------------------------------
CHECK_LABEL = "idp.platform/check"
CHECK_RUN_LABEL = "idp.platform/check-run"
CHECK_APP_LABEL = "idp.platform/source-app"
CHECK_SHA_LABEL = "idp.platform/source-sha"

# Kind ở phạm vi cụm — KHÔNG gắn namespace khi rewrite manifest sang namespace kiểm tra.
CLUSTER_SCOPED_KINDS = {"Namespace", "ClusterRole", "ClusterRoleBinding",
                        "StorageClass", "CustomResourceDefinition"}


def new_run_id(app: str) -> str:
    """Định danh duy nhất cho một lần kiểm. DNS-safe, đủ ngắn để làm hậu tố namespace."""
    stamp = time.strftime("%Y%m%d%H%M%S")
    rand = secrets.token_hex(3)
    return _slug(f"{app}-{stamp}-{rand}")[:40].rstrip("-")


@dataclass
class CheckRun:
    """Theo dõi + dọn tài nguyên tạm của MỘT lần deploy-check.

    Cleanup chỉ đụng tài nguyên CÓ nhãn run-id của chính lần chạy này. Trước khi xoá
    namespace, xác minh nó mang đúng nhãn check-run — không bao giờ xoá thứ mình không tạo.
    """
    app: str
    env: str
    sha: str
    run_id: str
    kubeconfig: str | None
    namespace: str
    vault_temp_policy: str = ""
    vault_temp_role: str = ""
    vault_temp_prefix: str = ""     # tiền tố KV (kèm mount) chứa secret thử, để xoá
    build_images: list = field(default_factory=list)  # ảnh tạm mang run-id nếu chạy --build
    leftovers: list = field(default_factory=list)

    def labels(self) -> dict:
        return {
            CHECK_LABEL: "true",
            CHECK_RUN_LABEL: self.run_id,
            CHECK_APP_LABEL: self.app,
            CHECK_SHA_LABEL: self.sha[:40],
        }

    # -- xác minh quyền sở hữu ------------------------------------------------------
    def _owns_namespace(self) -> bool:
        cp = kubectl(["get", "namespace", self.namespace, "-o", "json"],
                     kubeconfig=self.kubeconfig, check=False, capture=True)
        if cp.returncode != 0:
            return False
        labels = ((json.loads(cp.stdout or "{}").get("metadata") or {}).get("labels") or {})
        return labels.get(CHECK_RUN_LABEL) == self.run_id

    # -- dọn dẹp --------------------------------------------------------------------
    def cleanup(self) -> tuple[bool, list[str]]:
        """Xoá MỌI thứ lần chạy này tạo ra. Trả (thành công, danh sách còn sót).

        Chạy được nhiều lần (idempotent) và an toàn khi gọi từ khối finally kể cả lúc
        chưa kịp tạo gì. Chỉ xoá theo nhãn run-id + xác minh sở hữu namespace.
        """
        leftovers: list[str] = []

        # Vault trước (không phụ thuộc namespace) — chỉ role/policy/path mang run-id.
        if self.vault_temp_role:
            _vault_cleanup(f"auth/{_vault_str('auth_mount') or 'kubernetes'}/role/"
                           f"{self.vault_temp_role}", leftovers, f"vault role {self.vault_temp_role}")
        if self.vault_temp_policy:
            _vault_cleanup(f"sys/policies/acl/{self.vault_temp_policy}", leftovers,
                           f"vault policy {self.vault_temp_policy}")
        if self.vault_temp_prefix:
            _vault_delete_prefix(self.vault_temp_prefix, leftovers)

        # Namespace: xác minh sở hữu rồi xoá — cascade phần lớn tài nguyên trong nó
        # (Deployment, Pod, Service, HTTPRoute, PVC, SA, VaultAuth, VaultStaticSecret,
        # Secret, CNPG Cluster). Chỉ xoá khi nhãn check-run khớp run-id.
        cp = kubectl(["get", "namespace", self.namespace, "-o", "name"],
                     kubeconfig=self.kubeconfig, check=False, capture=True)
        if cp.returncode == 0:
            if not self._owns_namespace():
                leftovers.append(
                    f"namespace {self.namespace} (KHÔNG mang nhãn {CHECK_RUN_LABEL}="
                    f"{self.run_id} — TỪ CHỐI xoá, không phải của lần chạy này)")
            else:
                d = kubectl(["delete", "namespace", self.namespace, "--wait=false",
                             "--ignore-not-found"],
                            kubeconfig=self.kubeconfig, check=False, capture=True)
                if d.returncode != 0:
                    leftovers.append(f"namespace {self.namespace}: "
                                     f"{(d.stderr or '').strip()[:160]}")
                else:
                    log(f"đã xoá namespace kiểm tra {self.namespace}")

        # Ảnh tạm của --build: xoá BEST-EFFORT. Spec ràng buộc "nếu registry hỗ trợ xóa" —
        # một tag ảnh còn sót là vô hại (không phải tài nguyên cụm bị rò), nên nó chỉ CẢNH
        # BÁO, KHÔNG tính vào `leftovers` làm hỏng exit code. Chỉ k8s/Vault còn sót mới fail.
        for ref in self.build_images:
            _delete_temp_image(ref)

        ok = not leftovers
        return ok, leftovers


def _vault_cleanup(path: str, leftovers: list[str], label: str) -> None:
    status, _ = vault_api("DELETE", path, tolerate=(404,))
    if status in (200, 204, 404, -1):
        if status != -1:
            log(f"đã xoá {label}")
        return
    leftovers.append(label)


def _vault_delete_prefix(prefix: str, leftovers: list[str]) -> None:
    """Xoá metadata (kv-v2) hoặc toàn bộ key dưới một tiền tố KV thử nghiệm.

    prefix = '<mount>/<path>/' (kèm mount, kết thúc bằng '/'). Chỉ xoá đúng tiền tố mang
    run-id — không bao giờ chạm secret thật của app.
    """
    mount = _vault_str("kv_mount") or "kv"
    kv_type = (_vault_str("kv_type") or "kv-v2").lower()
    rest = prefix[len(mount) + 1:].rstrip("/")
    listing_base = f"{mount}/metadata" if kv_type == "kv-v2" else mount
    status, body = vault_api("LIST", f"{listing_base}/{rest}", tolerate=(404,))
    if status == -1:
        return
    keys = (body.get("data") or {}).get("keys") or [] if status == 200 else []
    del_base = f"{mount}/metadata" if kv_type == "kv-v2" else mount
    for key in keys:
        _vault_cleanup(f"{del_base}/{rest}/{key.rstrip('/')}", leftovers,
                       f"vault secret {rest}/{key}")


def _build_and_push(app_dir: Path, result: PipelineResult, catalog: Path,
                    run_state: CheckRun) -> None:
    """Build + push ảnh tạm cho từng workload theo ĐÚNG ref manifest đang trỏ.

    Dùng cho --build (kiểm cả source CHƯA commit). Context/Dockerfile lấy từ build_specs
    của catalog — cùng nguồn sự thật mà `image-plan --with-build` dùng, nên context của
    monorepo (gốc kho) không bị đoán sai. Ref build ra được ghi vào run_state để cleanup
    xoá ảnh sau kiểm tra.
    """
    if not shutil.which("docker"):
        raise DeployCheckError(
            "build ảnh tạm", "--build cần docker trên máy nhưng không thấy", LAYER_PLATFORM,
            "cài docker, hoặc bỏ --build và deploy-check một commit đã push", "which docker")
    specs = build_specs(Path(app_dir), result.services, catalog)
    for svc in result.services:
        ref = result.image_plan[svc.workload]
        spec = specs[svc.workload]
        context = str(Path(app_dir) / spec["context"])
        dockerfile = str(Path(app_dir) / spec["dockerfile"])
        b = run(["docker", "build", "-t", ref, "-f", dockerfile, context],
                check=False, capture=True)
        if b.returncode != 0:
            raise DeployCheckError(
                "build ảnh tạm", ((b.stderr or "") + (b.stdout or "")).strip()[:400],
                LAYER_SOURCE, f"Dockerfile của workload {svc.workload}",
                f"docker build -f {dockerfile} {context}")
        run_state.build_images.append(ref)  # ghi TRƯỚC push để cleanup dọn cả ảnh local
        psh = run(["docker", "push", ref], check=False, capture=True)
        if psh.returncode != 0:
            raise DeployCheckError(
                "push ảnh tạm", ((psh.stderr or "") + (psh.stdout or "")).strip()[:400],
                LAYER_PLATFORM, "đăng nhập registry / quyền push", f"docker push {ref}")
        log(f"đã build+push ảnh tạm {ref}")


def _delete_temp_image(image_ref: str) -> bool:
    """Xoá ảnh tạm mang run-id. BEST-EFFORT, không bao giờ làm fail lệnh.

    Thử theo thứ tự: skopeo (nếu có) -> registry HTTP delete API (registry:2 khi bật
    `storage.delete.enabled`). Không xoá được thì CẢNH BÁO kèm ref để prune tay — một tag
    ảnh còn sót không phải rò tài nguyên cụm, và spec chỉ yêu cầu xoá "nếu registry hỗ trợ".
    Đồng thời gỡ bản sao ảnh local (`docker rmi`) để không phình đĩa máy dev.
    """
    ok = False
    if shutil.which("skopeo"):
        cp = run(["skopeo", "delete", "--tls-verify=false", f"docker://{image_ref}"],
                 check=False, capture=True)
        ok = cp.returncode == 0
    if not ok:
        ok = _registry_delete(image_ref)
    if shutil.which("docker"):
        run(["docker", "rmi", "-f", image_ref], check=False, capture=True)  # bản local
    if ok:
        log(f"đã xoá ảnh tạm {image_ref}")
    else:
        warn(f"không xoá được ảnh tạm khỏi registry {image_ref} (thiếu skopeo và registry "
             f"không bật delete). Tag này vô hại; prune tay nếu cần.")
    return ok


def _registry_delete(image_ref: str) -> bool:
    """Xoá một tag khỏi registry:2 qua HTTP API. Trả True nếu xoá được. Best-effort.

    ref dạng `<host>/<repo>:<tag>`. Lấy digest bằng HEAD manifest (Accept v2), rồi DELETE
    theo digest. Registry phải bật `REGISTRY_STORAGE_DELETE_ENABLED=true`; nếu không, API
    trả 405 và ta coi như không hỗ trợ.
    """
    m = re.match(r"^([^/]+)/(.+):([^:/]+)$", image_ref)
    if not m or not shutil.which("curl"):
        return False
    host, repo, tag = m.group(1), m.group(2), m.group(3)
    scheme = "http" if host.startswith("localhost") or host.startswith("127.") else "https"
    accept = "application/vnd.docker.distribution.manifest.v2+json"
    head = run(["curl", "-sI", "-H", f"Accept: {accept}",
                f"{scheme}://{host}/v2/{repo}/manifests/{tag}"], check=False, capture=True)
    digest = ""
    for line in (head.stdout or "").splitlines():
        if line.lower().startswith("docker-content-digest:"):
            digest = line.split(":", 1)[1].strip()
            break
    if not digest:
        return False
    d = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "DELETE",
             f"{scheme}://{host}/v2/{repo}/manifests/{digest}"], check=False, capture=True)
    return (d.stdout or "").strip() in ("202", "200")


# --------------------------------------------------------------------------------------
# lệnh
# --------------------------------------------------------------------------------------
def cmd_pre_gitops(args) -> None:
    """Bước gộp TRƯỚC-GitOps cho deploy.yaml. Ghi audit theo từng stage như trước."""
    ident = None
    try:
        ident = _audit.identity_from(args.app, args.env,
                                     workflow=getattr(args, "workflow", "") or None)
    except Exception:
        ident = None

    def record(stage, status, message=None, category=None, duration_ms=None):
        if ident is None:
            return
        try:
            _audit.record_event(ident, stage=stage, status=status,
                                category=category, message=message,
                                duration_ms=duration_ms)
        except Exception:
            pass  # kho lịch sử không bao giờ được đánh sập deploy (fail-open)

    params = PipelineParams(
        app=args.app, env=args.env, app_dir=Path(args.app_dir), sha=args.tag,
        registry=args.registry, image=args.image or args.app,
        tag_strategy=args.tag_strategy, work=Path(args.work), out=Path(args.out),
        catalog=Path(args.catalog), kubeconfig=args.kubeconfig,
        accept_empty_database=getattr(args, "accept_empty_database", False),
        state_file=getattr(args, "state_file", None),
        no_state=getattr(args, "no_state", False),
        harbor_host=getattr(args, "harbor_host", "") or "",
        harbor_user=getattr(args, "harbor_user", "") or "",
        harbor_pass=getattr(args, "harbor_pass", "") or "",
        backup_key_id=getattr(args, "backup_key_id", "") or "",
        backup_secret_key=getattr(args, "backup_secret_key", "") or "",
        do_dry_run=not getattr(args, "skip_dry_run", False))
    try:
        run_pre_gitops(params, record=record)
    except DeployCheckError as err:
        print("\n" + format_diagnostic(err), file=sys.stderr, flush=True)
        record(err.stage, "failure", message=err.cause, category=err.layer)
        raise SystemExit(f"pre-gitops dừng ở: {err.stage}") from None
    log("pre-gitops OK — sẵn sàng cho bước GitOps")


def cmd_deploy_check(args) -> None:
    """Thử triển khai app từ máy lập trình viên vào namespace kiểm tra riêng, rồi dọn sạch.

    Chỉ hỗ trợ staging — TỪ CHỐI prod: đây là công cụ thử nghiệm, không được chạm đường
    production.
    """
    if args.env != "staging":
        raise SystemExit(
            f"deploy-check chỉ hỗ trợ staging, không phải {args.env!r}. Đây là công cụ thử "
            "trước khi push — nó tạo và xoá tài nguyên thật, nên cố ý không cho chạm prod.")

    app = validate_secret_name(args.app)
    env = validate_environment(args.env)
    app_dir = Path(args.app_dir)
    registry = args.registry or CONFIG.get("registry.path")
    if not registry:
        raise SystemExit("thiếu --registry và registry.path trong platform.env.yaml")
    catalog = Path(args.catalog) if args.catalog else REPO_ROOT
    build = getattr(args, "build", False)

    # run-id tính TRƯỚC mọi việc có thể hỏng, để chẩn đoán luôn in được Run ID và để
    # cleanup có định danh kể cả khi lỗi xảy ra ngay ở bước xác nhận source.
    run_id = new_run_id(app)
    namespace = f"{app}-check-{run_id}"[:63].rstrip("-")
    owns_work = not getattr(args, "work", None)
    work = Path(args.work) if not owns_work else \
        Path(tempfile.mkdtemp(prefix=f"deploycheck-{run_id}-"))
    run_state = CheckRun(app=app, env=env, sha=args.sha, run_id=run_id,
                         kubeconfig=args.kubeconfig, namespace=namespace)
    log(f"deploy-check {app}/{env} run-id={run_id} ns={namespace}")

    err: DeployCheckError | None = None
    ok, cleanup_note = True, "thành công"
    try:
        # bước 4: xác nhận source + SHA (trong try để lỗi cũng được format chẩn đoán)
        sha = _resolve_and_validate_sha(app_dir, args.sha, build=build)
        run_state.sha = sha

        # kiểm GitHub (chỉ thứ local xác minh được)
        github_checks(app, env, sha, check_pushed=not build)

        # namespace kiểm tra tạo SỚM, có nhãn sở hữu, để server-side dry-run của bản dùng
        # chung có đích thật mà nhắm vào.
        _create_labeled_namespace(namespace, run_state.labels(), args.kubeconfig)

        # ĐÚNG đường điều phối dùng chung như deploy.yaml — nhưng nhắm namespace kiểm tra,
        # và tắt vault/secrets của bản chung (deploy-check tự dựng phiên bản Vault THỬ +
        # secret trong _ephemeral_deploy để không chạm tài nguyên thật của app).
        temp_state = str(work / "check-state.yaml")
        # --build: manifest trỏ ảnh TẠM mang run-id (tag=run-id, strategy commit để mọi
        # workload chung một tag dễ build/push/xoá). Không build: dùng ảnh theo SHA.
        params = PipelineParams(
            app=app, env=env, app_dir=app_dir, sha=sha, registry=registry,
            image=args.image or app,
            tag_strategy="commit" if build else getattr(args, "tag_strategy", ""),
            render_tag=run_id if build else "",
            work=work, out=work / "manifests.yaml", catalog=catalog,
            kubeconfig=args.kubeconfig, target_namespace=namespace,
            state_file=temp_state,
            do_vault=False, do_secrets=False, do_dry_run=True)
        result = run_pre_gitops(params)

        # --build: build + push ảnh tạm ĐÚNG ref mà manifest vừa trỏ tới, TRƯỚC khi apply
        # để pod kéo được. Ghi lại ref để cleanup xoá ảnh sau kiểm tra.
        if build:
            _build_and_push(app_dir, result, catalog, run_state)

        # --- luồng thử ephemeral ---------------------------------------------------
        _ephemeral_deploy(run_state, result, work, args)

        log(f"[OK] deploy-check {app}/{env} sha={sha[:12]} — mọi bước đạt.")
    except DeployCheckError as e:
        err = e
    finally:
        ok, leftovers = run_state.cleanup()
        if owns_work:
            shutil.rmtree(work, ignore_errors=True)
        cleanup_note = "thành công" if ok else "CÒN SÓT:\n  - " + "\n  - ".join(leftovers)

    if err is not None:
        print("\n" + format_diagnostic(err, run_id=run_id, cleanup=cleanup_note),
              file=sys.stderr, flush=True)
        raise SystemExit(1)
    if not ok:
        print("\n[FAIL] cleanup", file=sys.stderr)
        print(f"Nguyên nhân: không xoá hết tài nguyên tạm", file=sys.stderr)
        print(f"Tầng lỗi: {LAYER_KUBERNETES}", file=sys.stderr)
        print(f"Run ID: {run_id}", file=sys.stderr)
        print(f"Cleanup: {cleanup_note}", file=sys.stderr)
        print("Sửa tại: xoá tay các tài nguyên còn sót ở trên, ví dụ:", file=sys.stderr)
        print(f"  kubectl delete namespace {namespace}", file=sys.stderr)
        raise SystemExit(2)
    log(f"deploy-check hoàn tất, cleanup {cleanup_note}. Run ID: {run_id}")


def _resolve_and_validate_sha(app_dir: Path, sha: str, *, build: bool) -> str:
    """Bước 4: xác nhận app_dir là git repo, HEAD khớp --sha, SHA tồn tại, cây sạch.

    Với --build, cho phép cây bẩn (ta build ảnh tạm từ source hiện tại) và bỏ ràng buộc
    HEAD==sha. Không có --build: ảnh theo SHA KHÔNG chứa thay đổi chưa commit, nên phải
    từ chối cây bẩn — báo thành công trên một ảnh không khớp source là đúng cái bẫy cần bắt.
    """
    if not (app_dir / ".git").exists():
        raise DeployCheckError(
            "xác nhận source", f"{app_dir} không phải kho Git",
            LAYER_SOURCE, "--app-dir (phải là checkout git của app)", "git -C <app-dir> status")

    head = run(["git", "rev-parse", "HEAD"], cwd=app_dir, check=False, capture=True)
    if head.returncode != 0:
        raise DeployCheckError(
            "xác nhận source", "không đọc được HEAD của kho", LAYER_SOURCE,
            "--app-dir", "git -C <app-dir> rev-parse HEAD")
    head_sha = head.stdout.strip()

    # SHA tồn tại trong repo?
    exists = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                 cwd=app_dir, check=False, capture=True)
    if exists.returncode != 0:
        raise DeployCheckError(
            "xác nhận commit", f"commit {sha[:12]} không có trong kho",
            LAYER_SOURCE, "--sha (phải là commit tồn tại trong app-dir)",
            f"git -C {app_dir} cat-file -e {sha}")
    full = run(["git", "rev-parse", f"{sha}^{{commit}}"],
               cwd=app_dir, check=False, capture=True).stdout.strip() or sha

    dirty = run(["git", "status", "--porcelain"], cwd=app_dir,
                check=False, capture=True).stdout.strip()
    if build:
        return full
    if head_sha != full:
        raise DeployCheckError(
            "xác nhận source", f"HEAD ({head_sha[:12]}) khác --sha ({full[:12]})",
            LAYER_SOURCE, "--sha, hoặc checkout đúng commit trong app-dir",
            f"git -C {app_dir} rev-parse HEAD")
    if dirty:
        raise DeployCheckError(
            "xác nhận source", "cây làm việc có thay đổi CHƯA COMMIT — ảnh theo SHA không "
            "chứa chúng, nên kiểm tra này sẽ nói dối. Dùng --build để kiểm source chưa commit.",
            LAYER_SOURCE, "commit thay đổi, hoặc chạy lại với --build",
            f"git -C {app_dir} status --porcelain")
    return full


def _ephemeral_deploy(run_state: CheckRun, result: PipelineResult, work: Path, args) -> None:
    """Triển khai thật vào namespace kiểm tra: Vault tạm → SA/VaultAuth/VSS → apply
    manifest → DB+PVC → chờ đồng bộ/Ready/rollout → kiểm Service+HTTPRoute.

    Toàn bộ tài nguyên mang nhãn run-id để cleanup nhận diện. Manifest được rewrite sang
    namespace kiểm tra và (khi có Vault) trỏ VaultStaticSecret vào tiền tố Vault THỬ, để
    không đụng secret thật của app.
    """
    app, env, ns = run_state.app, run_state.env, run_state.namespace
    kube = run_state.kubeconfig

    # namespace kiểm tra, gắn nhãn sở hữu
    _create_labeled_namespace(ns, run_state.labels(), kube)

    docs = load_all(result.manifests)
    has_vault = any(d.get("kind") == "VaultStaticSecret" for d in docs)
    has_db = any(d.get("kind") == "Cluster"
                 and str(d.get("apiVersion", "")).startswith("postgresql.cnpg.io/")
                 for d in docs)

    # --- Vault tạm: policy/role/secret thử + rewrite VSS sang tiền tố thử -------------
    if has_vault:
        _setup_temp_vault(run_state, docs)

    # rewrite mọi doc namespaced sang namespace kiểm tra + gắn nhãn run-id
    for d in docs:
        meta = d.setdefault("metadata", {})
        if d.get("kind") not in CLUSTER_SCOPED_KINDS:
            meta["namespace"] = ns
        labels = meta.setdefault("labels", {})
        labels.update(run_state.labels())
    # SA + VaultAuth cho namespace kiểm tra (dùng role tạm nếu có Vault)
    extra = _ephemeral_vault_auth(run_state) if has_vault else []
    applied = work / "check-apply.yaml"
    dump_all(extra + docs, applied)

    cp = kubectl(["apply", "-n", ns, "-f", str(applied)],
                 kubeconfig=kube, check=False, capture=True)
    if cp.returncode != 0:
        raise DeployCheckError(
            "apply vào namespace kiểm tra", ((cp.stderr or "") + (cp.stdout or "")).strip()[:400],
            LAYER_KUBERNETES, "manifest render / quyền trên namespace kiểm tra",
            f"kubectl apply -n {ns} -f {applied}")
    log(f"đã apply {len(docs)} manifest vào {ns}")

    timeout = getattr(args, "timeout", 300)
    wait_args = argparse.Namespace(app=app, env=env, kubeconfig=kube)
    # namespace kiểm tra khác app_namespace, nên các hàm chờ nhận ns tường minh. Các hàm
    # wait_* ném SystemExit — bọc lại thành DeployCheckError đúng tầng để cleanup + chẩn
    # đoán phân tầng vẫn chạy (SystemExit trần sẽ nhảy qua format chẩn đoán).
    if has_vault:
        _run_wait(wait_for_vault_secrets, docs, ns, wait_args, stage="chờ Vault đồng bộ",
                  layer=LAYER_VAULT,
                  fix_at="policy/role Vault, đường dẫn secret, hoặc VaultAuth trong namespace")
    if has_db:
        _run_wait(wait_for_databases, docs, ns, wait_args, stage="chờ database sẵn sàng",
                  layer=LAYER_DATABASE,
                  fix_at="CNPG operator, StorageClass/PVC, hoặc credential database")

    _wait_rollout(docs, ns, kube, timeout)
    _check_service_and_routes(docs, ns, kube)


def _run_wait(fn, docs, ns, args, *, stage: str, layer: str, fix_at: str) -> None:
    """Gọi một hàm chờ của engine, dịch SystemExit của nó sang DeployCheckError có tầng."""
    try:
        fn(docs, ns, args)
    except SystemExit as exc:
        raise DeployCheckError(stage, str(exc), layer, fix_at,
                               f"kubectl get all -n {ns}") from None


def _create_labeled_namespace(ns: str, labels: dict, kube: str | None) -> None:
    body = {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": ns, "labels": labels}}
    cp = kubectl(["create", "-f", "-"], kubeconfig=kube,
                 stdin=yaml.safe_dump(body), check=False, capture=True)
    if cp.returncode != 0 and "AlreadyExists" not in ((cp.stderr or "") + (cp.stdout or "")):
        raise DeployCheckError(
            "tạo namespace kiểm tra", (cp.stderr or "").strip()[:300], LAYER_KUBERNETES,
            "quyền tạo namespace trên cụm", f"kubectl create ns {ns}")
    log(f"tạo namespace kiểm tra {ns}")


def _setup_temp_vault(run_state: CheckRun, docs: list[dict]) -> None:
    """Tạo tiền tố Vault THỬ + policy/role tạm, ghi secret thử, rewrite VSS trong docs.

    Vì sao dùng tiền tố thử chứ không đọc secret thật: policy tạm chỉ cấp đọc đúng tiền tố
    mang run-id, nên lần kiểm không bao giờ chạm được secret production của app; và ta được
    phép ghi giá trị thử vào đó rồi xoá sạch. Giá trị thử KHÔNG BAO GIỜ log ra.
    """
    address = os.environ.get("VAULT_ADDR")
    token = os.environ.get("VAULT_TOKEN")
    if not address or not token:
        raise DeployCheckError(
            "cấu hình Vault thử", "app dùng Vault nhưng thiếu VAULT_ADDR/VAULT_TOKEN để "
            "dựng secret thử", LAYER_VAULT, "đặt VAULT_ADDR + VAULT_TOKEN (token có quyền ghi)",
            "vault token lookup")

    mount = _vault_str("kv_mount") or "kv"
    kv_type = (_vault_str("kv_type") or "kv-v2").lower()
    # tiền tố thử: apps/<app>/_check-<runid>/  — nằm dưới cùng gốc apps/ nhưng KHÁC env
    rel_prefix = f"apps/{run_state.app}/_check-{run_state.run_id}"
    run_state.vault_temp_prefix = f"{mount}/{rel_prefix}/"
    policy = f"idp-check-{run_state.run_id}"[:120]
    role = f"idp-check-{run_state.run_id}"[:120]
    run_state.vault_temp_policy = policy
    run_state.vault_temp_role = role

    # ghi secret thử cho từng VaultStaticSecret, rewrite path của nó sang tiền tố thử
    for d in docs:
        if d.get("kind") != "VaultStaticSecret":
            continue
        spec = d.setdefault("spec", {})
        logical = ((d.get("metadata") or {}).get("annotations") or {}).get(
            "idp.platform/logical-secret") or (d.get("metadata") or {}).get("name")
        keys = _keys_for_vss(d, docs)
        leaf = _slug(logical)
        rel_secret = f"{rel_prefix}/{leaf}"
        payload = {k: "deploy-check-placeholder" for k in keys} or {"value": "deploy-check"}
        # Nếu secret này là credential của một CNPG Cluster: `username` PHẢI khớp tên role
        # DB thật. CNPG initdb dùng chính `username` trong secret làm tên owner role — đặt
        # placeholder vào đó thì role đúng tên (vd app_backend, thứ app kết nối) KHÔNG có mật
        # khẩu placeholder, và app auth thất bại. Đo được trên cụm: asyncpg
        # InvalidPasswordError cho user "app_backend".
        dest_name = (spec.get("destination") or {}).get("name") or d["metadata"]["name"]
        owner = _db_owner_for_secret(dest_name, docs)
        if owner and "username" in payload:
            payload["username"] = owner
        data = {"data": payload} if kv_type == "kv-v2" else payload
        write_path = (f"{mount}/data/{rel_secret}" if kv_type == "kv-v2"
                      else f"{mount}/{rel_secret}")
        vault_api("PUT", write_path, data)
        spec["mount"] = mount
        spec["path"] = rel_secret
        spec["type"] = kv_type

    # policy đọc đúng tiền tố thử
    if kv_type == "kv-v2":
        hcl = (f'path "{mount}/data/{rel_prefix}/*" {{ capabilities = ["read"] }}\n'
               f'path "{mount}/metadata/{rel_prefix}/*" {{ capabilities = ["read","list"] }}\n')
    else:
        hcl = f'path "{mount}/{rel_prefix}/*" {{ capabilities = ["read","list"] }}\n'
    vault_api("PUT", f"sys/policies/acl/{policy}", {"policy": hcl})

    # role kubernetes bound vào SA + namespace kiểm tra
    vault_api("POST", f"auth/{_vault_str('auth_mount') or 'kubernetes'}/role/{role}", {
        "bound_service_account_names": [vault_service_account(run_state.app, run_state.env)],
        "bound_service_account_namespaces": [run_state.namespace],
        "token_policies": [policy],
        "token_ttl": _vault_str("token_ttl") or "1h",
        "audience": _vault_str("auth_audience") or "vault",
    })
    log(f"đã dựng Vault thử: policy/role {policy}, tiền tố {run_state.vault_temp_prefix}")


def _keys_for_vss(vss: dict, docs: list[dict]) -> list[str]:
    """Các key mà secret thử phải có để pod khởi động được.

    Nguồn chính xác nhất là chính manifest: Deployment tham chiếu Secret đích của
    VaultStaticSecret qua `secretKeyRef.key`. Nếu secret thử thiếu đúng key đó, pod kẹt
    `CreateContainerConfigError` — một lỗi GIẢ do bản kiểm tự gây ra, không phải lỗi của
    app. Nên ta gom đúng bộ key mà các Deployment yêu cầu từ Secret đích này.

    `envFrom.secretRef` (đổ nguyên Secret) không nêu key cụ thể — pod nhận mọi key có sẵn,
    nên không cần liệt kê; một secret thử tối thiểu là đủ.
    """
    spec = vss.get("spec") or {}
    dest = spec.get("destination") or {}
    dest_name = dest.get("name") or (vss.get("metadata") or {}).get("name")
    keys: set[str] = set()
    transform = dest.get("transformation") or {}
    # 1) template transformation (nếu có) nêu tường minh key đầu ra
    keys.update((transform.get("templates") or {}).keys())
    # 2) `includes` là các regex chọn key từ secret Vault (vd credential DB dùng
    #    `^username$`/`^password$`, type kubernetes.io/basic-auth). CNPG đọc THẲNG Secret này
    #    nên nó BẮT BUỘC có đủ username+password — Deployment không tham chiếu chúng qua
    #    secretKeyRef, nên nếu bỏ qua includes thì secret thử thiếu key và initdb chết
    #    (CreateContainerConfigError). Trích literal từ mẫu neo `^tên$`.
    for pat in transform.get("includes") or []:
        m = re.match(r"^\^([A-Za-z0-9_.-]+)\$$", str(pat))
        if m:
            keys.add(m.group(1))
    # secretKeyRef trong mọi Deployment trỏ vào Secret đích này
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        pod = d.get("spec", {}).get("template", {}).get("spec", {})
        for c in pod.get("containers", []) or []:
            for e in c.get("env", []) or []:
                ref = ((e.get("valueFrom") or {}).get("secretKeyRef") or {})
                if ref.get("name") == dest_name and ref.get("key"):
                    keys.add(ref["key"])
    return sorted(keys)


def _db_owner_for_secret(secret_name: str, docs: list[dict]) -> str | None:
    """Tên role/owner DB mà một credential secret phục vụ, lấy từ CNPG Cluster.

    CNPG dùng `bootstrap.initdb.owner` (và `managed.roles[].name`) làm tên role, và mật khẩu
    role lấy từ chính secret này. App kết nối bằng đúng tên đó (DB_USER là literal owner).
    Nên `username` của secret thử phải bằng owner, không phải một placeholder tuỳ ý.
    """
    for d in docs:
        if d.get("kind") != "Cluster" or not str(d.get("apiVersion", "")).startswith("postgresql.cnpg.io/"):
            continue
        spec = d.get("spec") or {}
        initdb = (spec.get("bootstrap") or {}).get("initdb") or {}
        if (initdb.get("secret") or {}).get("name") == secret_name and initdb.get("owner"):
            return initdb["owner"]
        for role in (spec.get("managed") or {}).get("roles") or []:
            if (role.get("passwordSecret") or {}).get("name") == secret_name and role.get("name"):
                return role["name"]
    return None


def _ephemeral_vault_auth(run_state: CheckRun) -> list[dict]:
    """SA + VaultAuth cho namespace kiểm tra, VaultAuth trỏ role TẠM."""
    ns = run_state.namespace
    sa = vault_service_account(run_state.app, run_state.env)
    labels = dict(run_state.labels())
    spec = {
        "method": "kubernetes",
        "mount": _vault_str("auth_mount") or "kubernetes",
        "vaultAuthGlobalRef": {
            "name": _vault_str("auth_global_name") or "default",
            "namespace": _vault_str("operator_namespace") or "vault-secrets-operator-system",
        },
        "kubernetes": {"role": run_state.vault_temp_role, "serviceAccount": sa},
    }
    audience = _vault_str("auth_audience")
    if audience:
        spec["kubernetes"]["audiences"] = [audience]
    if _vault_str("namespace"):
        spec["namespace"] = _vault_str("namespace")
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": sa, "namespace": ns, "labels": labels}},
        {"apiVersion": VAULT_API, "kind": "VaultAuth",
         "metadata": {"name": _vault_str("auth_ref") or "app-vault",
                      "namespace": ns, "labels": labels}, "spec": spec},
    ]


def _wait_rollout(docs: list[dict], ns: str, kube: str | None, timeout: int) -> None:
    """Chờ mọi Deployment chạy ĐÚNG ảnh vừa render (giống cmd_verify nhưng cho ns kiểm tra)."""
    want: dict[str, list[str]] = {}
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        containers = (d.get("spec", {}).get("template", {}).get("spec", {})
                      .get("containers", []))
        want[d["metadata"]["name"]] = [c.get("image") for c in containers]
    if not want:
        return
    log(f"chờ {len(want)} Deployment trong {ns} chạy đúng ảnh (tối đa {timeout}s)")
    deadline = time.time() + timeout
    pending: list[str] = []
    while True:
        pending = []
        for name, images in sorted(want.items()):
            cp = kubectl(["get", "deploy", name, "-n", ns, "-o", "json"],
                         kubeconfig=kube, check=False, capture=True)
            if cp.returncode != 0:
                pending.append(f"{name}: chưa tồn tại")
                continue
            obj = json.loads(cp.stdout)
            live = [c.get("image") for c in
                    obj["spec"]["template"]["spec"].get("containers", [])]
            if live != images:
                pending.append(f"{name}: chạy {live}, cần {images}")
                continue
            st = obj.get("status") or {}
            need = (obj.get("spec") or {}).get("replicas", 1) or 1
            gen = (obj.get("metadata") or {}).get("generation", 0) or 0
            if (st.get("observedGeneration", 0) or 0) < gen:
                pending.append(f"{name}: cụm chưa xử lý bản mới")
            elif (st.get("updatedReplicas", 0) or 0) < need:
                pending.append(f"{name}: chưa đủ bản sao mới")
            elif (st.get("availableReplicas", 0) or 0) < need:
                pending.append(f"{name}: chưa đủ bản sao sẵn sàng")
        if not pending:
            log(f"tất cả {len(want)} Deployment trong {ns} đã chạy đúng ảnh")
            return
        if time.time() >= deadline:
            break
        time.sleep(poll_interval(5))
    _dump_rollout_diagnostics([n for n in want], ns, kube)
    raise DeployCheckError(
        "chờ Deployment", "; ".join(pending), LAYER_KUBERNETES,
        "ảnh (đã build+push chưa?), tài nguyên cụm, hoặc secret/DB chưa sẵn sàng",
        f"kubectl get pods -n {ns}")


def _dump_rollout_diagnostics(deploys: list[str], ns: str, kube: str | None) -> None:
    """In pod, event và LOG của pod chưa sẵn sàng — để biết VÌ SAO, không chỉ 'chưa sẵn sàng'.

    Đây là điểm khác biệt của một chẩn đoán hữu ích: một backend CrashLoopBackOff cần thấy
    dòng log cuối (vd 'could not connect to database'), không phải chỉ trạng thái. In ra
    stderr/stdout để lọt vào transcript của lần chạy.
    """
    kubectl(["get", "pods", "-n", ns, "-o", "wide"], kubeconfig=kube, check=False)
    kubectl(["get", "events", "-n", ns, "--sort-by=.lastTimestamp"],
            kubeconfig=kube, check=False)
    cp = kubectl(["get", "pods", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}"],
                 kubeconfig=kube, check=False, capture=True)
    for pod in (cp.stdout or "").split():
        if not any(pod.startswith(dep + "-") for dep in deploys):
            continue  # bỏ pod của datastore (CNPG) — chỉ soi workload của app
        log(f"--- log {pod} (25 dòng cuối) ---")
        kubectl(["logs", pod, "-n", ns, "--tail=25", "--all-containers"],
                kubeconfig=kube, check=False)
        # --previous: nếu pod đã restart (CrashLoop), log lần chạy TRƯỚC mới là chỗ có lỗi
        kubectl(["logs", pod, "-n", ns, "--tail=25", "--all-containers", "--previous"],
                kubeconfig=kube, check=False)


def _check_service_and_routes(docs: list[dict], ns: str, kube: str | None) -> None:
    """Kiểm Service tồn tại và HTTPRoute đã attach (có parent Accepted)."""
    for d in docs:
        kind = d.get("kind")
        name = (d.get("metadata") or {}).get("name")
        if kind == "Service":
            cp = kubectl(["get", "service", name, "-n", ns, "-o", "name"],
                         kubeconfig=kube, check=False, capture=True)
            if cp.returncode != 0:
                raise DeployCheckError(
                    "kiểm Service", f"Service {name} không lên trong {ns}",
                    LAYER_KUBERNETES, "manifest render (Service)", f"kubectl get svc -n {ns}")
        if kind == "HTTPRoute":
            cp = kubectl(["get", "httproute", name, "-n", ns, "-o", "json"],
                         kubeconfig=kube, check=False, capture=True)
            if cp.returncode != 0:
                raise DeployCheckError(
                    "kiểm HTTPRoute", f"HTTPRoute {name} không lên trong {ns}",
                    LAYER_NETWORK, "manifest render (HTTPRoute)", f"kubectl get httproute -n {ns}")
            obj = json.loads(cp.stdout or "{}")
            parents = (obj.get("status") or {}).get("parents") or []
            accepted = any(
                c.get("type") == "Accepted" and c.get("status") == "True"
                for pr in parents for c in (pr.get("conditions") or []))
            if parents and not accepted:
                raise DeployCheckError(
                    "attach HTTPRoute", f"HTTPRoute {name} chưa được Gateway chấp nhận",
                    LAYER_NETWORK, "ingress.gateway_name/gateway_namespace/section_name",
                    f"kubectl get httproute {name} -n {ns} -o yaml")
    log("Service/HTTPRoute đạt")
