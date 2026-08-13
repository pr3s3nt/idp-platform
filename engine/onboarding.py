"""Application lifecycle and the disabled-by-config onboarding state machine."""
from __future__ import annotations

from . import context as _context
from . import resources as _resources
from . import values as _values
from . import catalog as _catalog
from . import render as _render
from . import delivery as _delivery
for _module in (_context, _resources, _values, _catalog, _render, _delivery):
    globals().update({n: getattr(_module, n) for n in dir(_module) if not n.startswith("__")})

# =======================================================================================
# PHASE 6 — onboarding: một request, một máy trạng thái, chạy lại được
# =======================================================================================
# Mục 13 của kế hoạch. Điều cần giữ trong đầu khi đọc phần này:
#
# 1. MỖI BƯỚC KIỂM TRƯỚC KHI TẠO, và ghi state NGAY khi tạo xong. Một lần chạy hỏng ở
#    bước 7 rồi chạy lại phải TIẾP TỤC từ bước 7, không được tạo kho thứ hai, namespace
#    thứ hai hay một mật khẩu database mới đè lên cái database đang dùng.
# 2. TRẠNG THÁI NẰM NGOÀI TIẾN TRÌNH. Nó là một ConfigMap trong cụm (hoặc một file khi
#    chạy offline), nên một lần chạy khác — máy khác, người khác — nhìn thấy đúng thứ
#    lần trước để lại. Không dùng Secret: bản ghi này cố ý KHÔNG chứa giá trị bí mật nào.
# 3. KHÔNG BÁO READY SAI. Thiếu bí mật của bên thứ ba thì trạng thái là
#    WAITING_FOR_USER_SECRETS kèm đúng lệnh phải chạy — không phải một lần verify chờ hết
#    giờ rồi fail với "0/1 replicas ready", vốn gửi người trực đi soi image.
# 4. HAI NỬA QUYỀN, HAI CHỦ SỞ HỮU (mục 13.5). Thao tác GitHub chạy bằng danh tính người
#    dùng (`gh`); thao tác Vault cần token Vault RIÊNG. Không suy ra cái này từ cái kia:
#    quyền tạo repo không được kéo theo quyền viết policy trên Vault.
ONBOARD_API_VERSION = "idp.company/v1"
ONBOARD_KIND = "OnboardingRequest"

# Máy trạng thái ở mục 13.2. Thứ tự là thứ tự thật — engine đi tuần tự và không nhảy cóc.
ONBOARD_STATES = (
    "REQUESTED", "VALIDATING", "SCAFFOLDING_REPOSITORY", "BOOTSTRAPPING_PLATFORM",
    "CONFIGURING_VAULT", "PROVISIONING_DATABASE", "BUILDING_IMAGES", "DEPLOYING_STAGING",
    "VERIFYING_STAGING", "STAGING_READY", "PENDING_PROD_ACTIVATION", "PROVISIONING_PROD",
    "PENDING_PROD_APPROVAL", "VERIFYING_PROD", "READY",
)
# Nhánh tuỳ chọn: không phải lỗi, nhưng cũng KHÔNG phải READY.
ONBOARD_BRANCH_STATES = ("WAITING_FOR_USER_SECRETS", "PARTIALLY_READY", "FAILED_RETRYABLE")
# Vòng đời XOÁ (mục 13.4). Cố ý tách khỏi ONBOARD_STATES: xoá không phải "bước tiếp theo"
# của onboarding, nó là một workflow riêng có preview và có người duyệt.
ONBOARD_DELETE_STATES = ("DELETE_PLANNED", "PENDING_DELETE_APPROVAL", "DELETING", "DELETED")


class OnboardingPaused(Exception):
    """Dừng đúng chỗ, có trạng thái riêng, và chạy lại được — không phải lỗi.

    Hai chỗ dùng: thiếu bí mật của người dùng, và chờ người duyệt pull request prod. Cả
    hai đều là "đang chờ CON NGƯỜI", nên biến chúng thành lỗi sẽ dạy người vận hành bỏ
    qua lỗi của công cụ này.
    """

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state
        self.message = message


def onboarding_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------------ request (13.1)
def _req_block(doc: dict, key: str, where: str) -> dict:
    value = doc.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SystemExit(f"{where}: '{key}' phải là một mapping, đang là {type(value).__name__}.")
    return value


def _req_bool(value, key: str, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SystemExit(
            f"{where}: '{key}' phải là true/false, đang là {value!r}. Chuỗi \"false\" là "
            "một giá trị ĐÚNG trong YAML nên nó sẽ được hiểu là bật."
        )
    return value


def validate_onboarding_request(doc, where: str, catalog=None) -> dict:
    """Kiểm request onboarding và trả về bản đã chuẩn hoá (mục 13.1).

    Khoá lạ bị TỪ CHỐI chứ không bỏ qua. Một request là thứ người ta gõ tay một lần rồi
    quên; gõ nhầm `enviroments:` mà công cụ im lặng thì app được dựng thiếu prod và không
    ai biết cho tới lúc cần lên production.
    """
    if not isinstance(doc, dict):
        raise SystemExit(f"{where}: request phải là một YAML mapping.")
    known = {"apiVersion", "kind", "application", "stack", "database", "routing",
             "environments"}
    unknown = sorted(set(doc) - known)
    if unknown:
        raise SystemExit(
            f"{where}: khoá không nhận ra: {', '.join(unknown)}. "
            f"Được phép: {', '.join(sorted(known))}."
        )
    if doc.get("apiVersion") not in (None, ONBOARD_API_VERSION):
        raise SystemExit(f"{where}: apiVersion phải là {ONBOARD_API_VERSION!r}.")
    if doc.get("kind") not in (None, ONBOARD_KIND):
        raise SystemExit(f"{where}: kind phải là {ONBOARD_KIND!r}.")

    application = _req_block(doc, "application", where)
    name = validate_app_name(str(application.get("name") or ""))
    owner = str(application.get("owner") or "").strip()
    if not owner:
        raise SystemExit(
            f"{where}: application.owner là bắt buộc. Không có chủ sở hữu thì không ai "
            "nhận được cảnh báo, không ai duyệt được prod, và không ai xoá được app."
        )
    allowed_owners = CONFIG.get("onboarding.allowed_owners") or []
    if allowed_owners and owner not in allowed_owners:
        raise SystemExit(
            f"{where}: đội {owner!r} không nằm trong onboarding.allowed_owners của "
            "platform.env.yaml. Đây là chỗ kiểm quyền onboarding (mục 13.5) — sửa cấu "
            "hình, đừng sửa request."
        )

    stack_block = _req_block(doc, "stack", where)
    stack_id = str(stack_block.get("id") or "").strip()
    if not stack_id:
        raise SystemExit(f"{where}: stack.id là bắt buộc (xem `idpctl stack-list`).")
    stack = load_stack(catalog or REPO_ROOT, stack_id)
    published = str((stack.get("metadata") or {}).get("version"))
    wanted = str(stack_block.get("version") or "").strip()
    if not wanted:
        raise SystemExit(
            f"{where}: stack.version là bắt buộc. Bỏ trống nghĩa là 'phiên bản nào cũng "
            f"được', và app sẽ được sinh ra từ một bộ file khác nhau tuỳ ngày chạy. "
            f"Catalog này phát hành {stack_id} v{published}."
        )
    if wanted != published:
        raise SystemExit(
            f"{where}: xin stack {stack_id} v{wanted} nhưng catalog này phát hành "
            f"v{published}. Sửa request, hoặc dùng một catalog khác — đừng để hai bên lệch."
        )

    database = _req_block(doc, "database", where)
    has_capability = "database" in ((stack.get("spec") or {}).get("capabilities") or [])
    enabled = _req_bool(database.get("enabled"), "database.enabled", where, has_capability)
    if enabled != has_capability:
        raise SystemExit(
            f"{where}: database.enabled={enabled} nhưng stack {stack_id} "
            f"{'có' if has_capability else 'KHÔNG có'} capability `database`. Capability là "
            "thuộc tính của stack, không phải một công tắc theo app — chọn stack khác "
            "(`stack-list`) thay vì đổi cờ này."
        )
    profile = str(database.get("profile") or "application")
    if enabled and profile != "application":
        raise SystemExit(
            f"{where}: database.profile={profile!r} không được hỗ trợ. Platform này chỉ "
            "phát hành `application` — class cũ chỉ dùng để chạy thử và bị chặn ở prod."
        )

    routing = _req_block(doc, "routing", where)
    visibility = str(routing.get("visibility") or "internal")
    allowed_vis = CONFIG.get("onboarding.visibilities") or ["internal"]
    if visibility not in allowed_vis:
        raise SystemExit(
            f"{where}: routing.visibility={visibility!r} không nằm trong "
            f"onboarding.visibilities ({', '.join(allowed_vis)}). Catalog này chỉ có một "
            "Gateway; muốn thêm mức hiển thị khác thì thêm gateway + khai vào cấu hình, "
            "đừng nới ở đây."
        )

    envs_block = _req_block(doc, "environments", where)
    bad = sorted(set(envs_block) - set(ENVIRONMENTS))
    if bad:
        raise SystemExit(
            f"{where}: môi trường không tồn tại: {', '.join(bad)}. "
            f"Platform có đúng {', '.join(ENVIRONMENTS)}."
        )
    environments = {e: _req_bool(envs_block.get(e), f"environments.{e}", where, e == "staging")
                    for e in ENVIRONMENTS}
    if not environments["staging"]:
        raise SystemExit(
            f"{where}: environments.staging phải là true. Prod chỉ nhận ảnh ĐÃ verify ở "
            "staging, nên một app chỉ-prod không có đường nào hợp lệ để lên."
        )
    for env in ENVIRONMENTS:
        if environments[env] and enabled and not CONFIG.get(
                f"database_profiles.{env}.{profile}"):
            raise SystemExit(
                f"{where}: xin database ở {env} nhưng platform.env.yaml không có "
                f"database_profiles.{env}.{profile}."
            )

    return {
        "application": {"name": name, "owner": owner,
                        "description": str(application.get("description") or "")},
        "stack": {"id": stack_id, "version": published},
        "database": {"enabled": enabled, "profile": profile},
        "routing": {"visibility": visibility},
        "environments": environments,
    }


def load_onboarding_request(path, catalog=None) -> dict:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"không thấy file request: {p}")
    try:
        doc = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise SystemExit(f"{p}: YAML hỏng — {exc}") from None
    return validate_onboarding_request(doc, str(p), catalog)


def onboarding_idempotency_key(request: dict) -> str:
    """Băm của CHÍNH request đã chuẩn hoá (mục 13.4).

    Cùng một file chạy lại = cùng khoá = tiếp tục bản ghi cũ. Sửa request rồi chạy lại =
    khoá khác, và engine DỪNG thay vì âm thầm dựng lại một app đang chạy theo hình dạng
    mới. Đổi stack version của một app đang sống là một cuộc nâng cấp có pull request
    (`stack-upgrade`), không phải một lần onboarding thứ hai.
    """
    return hashlib.sha256(canonical_json(request).encode()).hexdigest()[:32]


def onboarding_request_id(request: dict) -> str:
    return f"ob-{request['application']['name']}-{onboarding_idempotency_key(request)[:8]}"


def onboarding_labels(request: dict, *, env: str = "") -> dict:
    """Nhãn mục 13.4 gắn lên mọi tài nguyên onboarding tạo ra.

    Có nhãn thì "cái này của ai, thuộc lần onboarding nào" trả lời được bằng một lệnh
    `kubectl get -l`, kể cả khi bản ghi state đã mất.
    """
    labels = {
        "app.kubernetes.io/part-of": "idp-platform",
        "idp.platform/application": request["application"]["name"],
        "idp.platform/stack-version": str(request["stack"]["version"]),
        "idp.platform/onboarding-request-id": onboarding_request_id(request),
    }
    if env:
        labels["idp.platform/environment"] = env
    return labels


# --------------------------------------------------------------- bản ghi state và audit
# Vì sao là ConfigMap chứ không phải Secret: bản ghi này CỐ Ý không chứa giá trị bí mật —
# chỉ tên đường dẫn Vault, tên kho, tên ảnh, trạng thái. Cất nó vào Secret sẽ dạy người
# đọc rằng "trong này có bí mật", và rồi sẽ có người viết bí mật thật vào.
#
# Vì sao nằm trong cụm chứ không phải trong repo cấu hình: một lần onboarding hỏng giữa
# chừng thường hỏng TRƯỚC khi repo cấu hình kịp tồn tại. State phải sống ở nơi có sẵn từ
# bước đầu tiên, và phải đọc được từ một máy khác — người mở lại việc dở dang hiếm khi là
# người bỏ dở nó.
class OnboardingStore:
    def read(self) -> dict | None:
        raise NotImplementedError

    def write(self, record: dict) -> None:
        raise NotImplementedError


class FileOnboardingStore(OnboardingStore):
    """Cho test và cho lần chạy khan (chưa có cụm). Cùng ngữ nghĩa, khác chỗ cất."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self) -> dict | None:
        if not self.path.is_file() or not self.path.stat().st_size:
            return None
        return json.loads(self.path.read_text())

    def write(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False))


class ClusterOnboardingStore(OnboardingStore):
    def __init__(self, app: str, kubeconfig: str | None = None):
        pattern = CONFIG.get("onboarding.state_configmap_pattern") or "idp-onboarding-{app}"
        self.name = pattern.replace("{app}", app)
        self.namespace = state_ns()
        self.kubeconfig = kubeconfig

    def read(self) -> dict | None:
        cp = kubectl(["get", "configmap", self.name, "-n", self.namespace, "-o", "json"],
                     kubeconfig=self.kubeconfig, check=False, capture=True)
        if cp.returncode != 0:
            return None
        data = (json.loads(cp.stdout).get("data") or {}).get("record.json")
        return json.loads(data) if data else None

    def write(self, record: dict) -> None:
        ensure_namespace(self.namespace, self.kubeconfig)
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": self.name, "namespace": self.namespace,
                         "labels": record.get("labels") or {}},
            "data": {"record.json": json.dumps(record, indent=2, sort_keys=True,
                                               ensure_ascii=False)},
        }
        cp = kubectl(["apply", "-f", "-"], kubeconfig=self.kubeconfig,
                     stdin=json.dumps(body), check=False, capture=True)
        if cp.returncode != 0:
            raise SystemExit(f"không ghi được state onboarding: {(cp.stderr or '').strip()}")


def make_onboarding_store(app: str, args) -> OnboardingStore:
    path = getattr(args, "state_file", None)
    return FileOnboardingStore(path) if path else ClusterOnboardingStore(
        app, getattr(args, "kubeconfig", None))


def new_onboarding_record(request: dict) -> dict:
    return {
        "recordVersion": 1,
        "requestId": onboarding_request_id(request),
        "idempotencyKey": onboarding_idempotency_key(request),
        "request": request,
        "labels": onboarding_labels(request),
        "state": "REQUESTED",
        "steps": {},
        "outputs": {},
        "history": [{"at": onboarding_now(), "state": "REQUESTED"}],
        "createdAt": onboarding_now(),
        "updatedAt": onboarding_now(),
    }


def load_or_create_record(store: OnboardingStore, request: dict) -> dict:
    """Bản ghi cho request này, tạo nếu chưa có — và TỪ CHỐI nếu request đã đổi.

    Đây là chỗ chặn "bản sao thứ hai". Không có nó, sửa một dòng trong request rồi chạy
    lại sẽ dựng thêm kho, thêm namespace, thêm credential database bên cạnh app đang
    chạy — mà mọi bước đều báo thành công.
    """
    existing = store.read()
    if existing is None:
        record = new_onboarding_record(request)
        store.write(record)          # GHI NGAY, trước khi tạo bất cứ thứ gì bên ngoài
        log(f"tạo bản ghi onboarding {record['requestId']}")
        return record
    if existing.get("idempotencyKey") != onboarding_idempotency_key(request):
        raise SystemExit(
            f"app {request['application']['name']!r} đã có một lần onboarding đang chạy "
            f"({existing.get('requestId')}, trạng thái {existing.get('state')}) với một "
            "request KHÁC. Chạy tiếp bằng đúng file request cũ, hoặc — nếu thật sự muốn "
            "đổi stack/capability của một app đang sống — dùng `stack-upgrade` và một "
            "pull request. Onboarding lần hai không phải cách nâng cấp."
        )
    log(f"tiếp tục bản ghi onboarding {existing['requestId']} "
        f"(trạng thái {existing.get('state')})")
    return existing


def record_state(record: dict, state: str, store: OnboardingStore) -> None:
    if record.get("state") != state:
        record.setdefault("history", []).append({"at": onboarding_now(), "state": state})
        # Lịch sử chỉ để đọc khi truy vết, không phải audit log — cắt bớt để bản ghi không
        # phình vô hạn sau vài trăm lần retry.
        record["history"] = record["history"][-50:]
    record["state"] = state
    record["updatedAt"] = onboarding_now()
    store.write(record)


# ---------------------------------------------------------------- workflow CI của app
def app_ci_workflow_template(catalog, workloads: int) -> Path:
    """Mẫu nào: một service hay nhiều service.

    Chọn nhầm mẫu hỏng ngay ở bước build ("failed to read dockerfile"), nên nó được suy ra
    từ SỐ WORKLOAD thật trong kho chứ không phải do người điền.
    """
    name = "app-ci-mot-service.yaml" if workloads <= 1 else "app-ci-nhieu-service.yaml"
    path = Path(catalog) / "templates" / name
    if not path.is_file():
        raise SystemExit(f"không thấy mẫu CI {path}")
    return path


def render_app_ci_workflow(text: str, *, app: str, image: str, registry: str,
                           platform_repo: str) -> str:
    """Điền bốn dòng đánh dấu `<-- SỬA` trong khối env của mẫu.

    Bốn giá trị đó là TOẠ ĐỘ (registry nào, kho platform nào) cộng danh tính app, nên
    chúng tới từ platform.env.yaml. Nếu một dòng nào đó không được thay, hàm này DỪNG:
    một workflow còn `REGISTRY: harbor.vi-du.vn/idp` sẽ chạy, sẽ đỏ ở bước push, và thông
    báo lỗi sẽ nói về mạng chứ không nói về việc quên điền.
    """
    values = {"APP": app, "IMAGE_NAME": image, "REGISTRY": registry,
              "PLATFORM_REPO": platform_repo}
    # Bỏ khối hướng dẫn ở đầu mẫu ("chép file này vào…, sửa 4 dòng…"). Nó nói với NGƯỜI
    # đang chọn mẫu bằng tay; trong một file đã được sinh ra thì nó là lời khuyên sai —
    # bốn dòng đó đã được điền, và người đọc tiếp theo sẽ đi tìm thứ không còn ở đó.
    lines = text.splitlines()
    fences = [i for i, line in enumerate(lines) if line.startswith("# ====")]
    if len(fences) >= 2 and any("MẪU CI" in line for line in lines[:fences[1]]):
        lines = lines[fences[1] + 1:]
    # Nhánh kích hoạt CI phải là ĐÚNG hai nhánh mà platform coi là staging và prod.
    # Mẫu viết sẵn `[dev, main]` vì đó là quy ước mặc định; một công ty đổi
    # `environments.staging.config_branch` mà CI vẫn nghe `dev` thì onboarding đẩy code lên
    # một nhánh không có workflow nào chạy — không ảnh nào được build, và không có lỗi ở đâu.
    # GitHub phân giải khối `on:` tĩnh nên CI không tự hỏi được; điền lúc sinh là chỗ duy nhất.
    branches = [str(CONFIG.get(f"environments.{env}.config_branch")
                    or CONFIG.get("git.default_branch", "main")) for env in ENVIRONMENTS]
    out, filled = [], set()
    for line in lines:
        m = re.match(r"^(\s{2})([A-Z_]+):\s.*$", line)
        if m and m.group(2) in values:
            out.append(f"{m.group(1)}{m.group(2)}: {values[m.group(2)]}")
            filled.add(m.group(2))
            continue
        m = re.match(r"^(\s*)branches:\s*\[.*\]\s*$", line)
        if m:
            out.append(f"{m.group(1)}branches: [{', '.join(branches)}]")
            filled.add("branches")
            continue
        out.append(line)
    missing = sorted((set(values) | {"branches"}) - filled)
    if missing:
        raise SystemExit(
            f"mẫu CI không có dòng cho {', '.join(missing)} trong khối env — mẫu và bộ "
            "sinh đã lệch nhau. Sửa templates/app-ci-*.yaml, đừng nới chỗ này."
        )
    rendered = "\n".join(out) + "\n"
    if "SỬA" in rendered:
        raise SystemExit(
            "workflow sinh ra vẫn còn chỗ đánh dấu phải sửa tay. Mẫu đã đổi hình dạng và "
            "bộ sinh không theo kịp — sửa templates/app-ci-*.yaml hoặc "
            "render_app_ci_workflow, đừng giao cho đội ứng dụng một file nửa vời."
        )
    banner = (
        f"# CI của {app} — DO PLATFORM SINH RA lúc onboarding, từ "
        "templates/app-ci-*.yaml.\n"
        "#\n"
        "# Sửa được: thêm bước test, đổi nhãn runner, thêm job. Lần onboarding sau KHÔNG\n"
        "# ghi đè file này.\n"
        "# Đừng tự tính tên ảnh hay context build — hỏi platform (`image-plan --with-build`),\n"
        "# vì quy tắc đó phải giống hệt cái orchestrator dùng khi render manifest.\n"
    )
    return banner + rendered


APP_CI_REL = ".github/workflows/ci.yaml"


def ci_branch_warnings(catalog) -> list[str]:
    """Những gì CI của app sẽ THẤY, kiểm ngay lúc sinh file. Trả về danh sách cảnh báo.

    CI của app checkout platform ở NHÁNH MẶC ĐỊNH (`ref: main` trong mẫu), không phải ở
    nhánh bạn đang đứng — cố ý, để CI và orchestrator luôn dùng cùng một bản renderer
    (orchestrator cũng chỉ chạy được từ nhánh mặc định). Hệ quả ít ai nghĩ tới: onboard một
    app **từ một nhánh chưa merge** sẽ giao cho đội ứng dụng một workflow gọi những thứ
    nhánh mặc định chưa có.

    Đo được trên GitHub, chính vì thiếu cảnh báo này: CI của app fixture đỏ ở bước đầu với
    `unrecognized arguments: --with-build`, và thông báo đó không hề nhắc tới việc nhánh
    chưa được merge. Lần thứ hai thì chạy được nhưng tính tag `content` trong khi
    orchestrator tính `commit`, vì `platform.env.yaml` trên nhánh mặc định còn tắt cờ —
    hai tag khác nhau cho một commit, và Fleet apply một ảnh chưa ai đẩy lên.

    Đọc bằng `git show <nhánh>:<file>`, không gọi mạng: thứ CI nhận được chính là nội dung
    đã commit trên nhánh đó.
    """
    catalog = Path(catalog)
    branch = str(CONFIG.get("git.default_branch", "main") or "main")
    out: list[str] = []

    def committed(rel: str) -> str | None:
        cp = run(["git", "show", f"{branch}:{rel}"], cwd=catalog, check=False, capture=True)
        return cp.stdout if cp.returncode == 0 else None

    renderer = committed("engine/cli.py")
    if renderer is None:
        return [f"không đọc được nhánh '{branch}' của catalog để kiểm xem CI của app sẽ "
                f"chạy bằng bản engine/cli.py nào. Tự kiểm trước khi giao kho cho đội "
                f"ứng dụng."]
    if "--with-build" not in renderer:
        out.append(
            f"nhánh '{branch}' của kho platform CHƯA có `image-plan --with-build`, mà CI "
            f"sinh ra thì gọi nó. Workflow này sẽ ĐỎ ngay ở bước đầu ('unrecognized "
            f"arguments') cho tới khi nhánh phát triển được merge. Merge trước, rồi hãy "
            f"onboard app mới.")

    config_text = committed("platform.env.yaml")
    if config_text is not None:
        try:
            shipped = EnvConfig(yaml.safe_load(config_text) or {})
        except yaml.YAMLError:
            shipped = None
        if shipped is not None and not shipped.get("features.stack_onboarding", False) \
                and feature("stack_onboarding"):
            out.append(
                f"`features.stack_onboarding` đang BẬT ở cấu hình bạn chạy nhưng TẮT trong "
                f"platform.env.yaml trên nhánh '{branch}'. CI của app đọc file trên nhánh "
                f"đó, nên nó sẽ tính tag `content` trong khi orchestrator tính `commit`: "
                f"hai tag khác nhau cho một commit, và Fleet apply một ảnh chưa ai đẩy lên.")
    return out


def write_app_ci_workflow(app_dir, app: str, *, catalog=None, image: str = "",
                          force: bool = False) -> bool:
    """Sinh `.github/workflows/ci.yaml` cho kho ứng dụng. True nếu có ghi.

    KHÔNG ghi đè file đã có: sau lần đầu, file này thuộc về đội ứng dụng — họ được thêm
    bước test, và một lần onboarding chạy lại không có quyền xoá việc đó.
    """
    app_dir = Path(app_dir)
    dest = app_dir / APP_CI_REL
    if dest.exists() and not force:
        log(f"{APP_CI_REL} đã có -> giữ nguyên")
        return False
    catalog = Path(catalog or REPO_ROOT)
    services = discover(app_dir)
    text = app_ci_workflow_template(catalog, len(services)).read_text()
    rendered = render_app_ci_workflow(
        text, app=app, image=image or app,
        registry=str(CONFIG.require("registry.path")),
        platform_repo=str(CONFIG.require("git.platform_repo")),
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(rendered)
    log(f"sinh {APP_CI_REL} ({len(services)} workload)")
    for message in ci_branch_warnings(catalog):
        warn(message)
    return True


# ------------------------------------------------------------------------ Vault (13.5)
# Nửa Vault của onboarding. Nó KHÔNG dùng cùng danh tính với nửa GitHub, và điều đó là cố
# ý: quyền tạo repo không được suy ra thành quyền viết policy. Token tới từ môi trường
# (VAULT_TOKEN), không bao giờ từ cấu hình — một token trong file cấu hình là một token
# nằm trong git.
def vault_api(method: str, path: str, payload: dict | None = None,
              *, tolerate: tuple[int, ...] = (),
              content_type: str = "application/json") -> tuple[int, dict]:
    """Một lần gọi Vault. Trả (mã HTTP, body). Không bao giờ log body — nó chứa bí mật."""
    address = (os.environ.get("VAULT_ADDR") or "").rstrip("/")
    token = os.environ.get("VAULT_TOKEN")
    if not address or not token:
        raise SystemExit(
            "VAULT_ADDR và VAULT_TOKEN phải được đặt cho phần Vault của onboarding. "
            "Cố ý không đọc từ platform.env.yaml: `vault.address` là địa chỉ CỤM nhìn "
            "thấy (thường không tới được từ máy đang chạy lệnh), và token thì không bao "
            "giờ được nằm trong một file trong git. Xem `vault-onboard --print-policy` "
            "nếu người quản trị Vault muốn tự chạy phần này."
        )
    headers = {"X-Vault-Token": token, "Content-Type": content_type}
    if _vault_str("namespace"):
        headers["X-Vault-Namespace"] = _vault_str("namespace")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{address}/v1/{path}", data=data, headers=headers,
                                     method=method)
    log(f"vault {method} {path}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode() or "{}"
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code in tolerate:
            return exc.code, {}
        detail = exc.read().decode(errors="replace")[:300]
        raise SystemExit(f"Vault từ chối {method} {path} ({exc.code}): {detail}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"không tới được Vault: {exc.reason}") from None


def vault_secret_key_names(app: str, env: str, name: str) -> set[str] | None:
    """TÊN các khoá trong một secret, hoặc None nếu đường dẫn chưa tồn tại.

    Chỉ tên, không bao giờ giá trị — hàm này tồn tại để trả lời "người dùng đã nạp bí mật
    chưa", và câu trả lời đó không cần biết bí mật là gì. Giá trị đọc về nằm trong biến
    cục bộ và không đi đâu cả; không log, không ghi vào state, không trả ra.
    """
    mount = _vault_str("kv_mount") or "kv"
    kv_type = (_vault_str("kv_type") or "kv-v2").lower()
    rel = vault_relative_path(app, env, name)
    path = f"{mount}/data/{rel}" if kv_type == "kv-v2" else f"{mount}/{rel}"
    status, body = vault_api("GET", path, tolerate=(404,))
    if status == 404:
        return None
    data = body.get("data") or {}
    if kv_type == "kv-v2":
        data = data.get("data") or {}
    return set(data)


def ensure_vault_app_access(app: str, env: str) -> dict:
    """Policy đọc/ghi + role kubernetes cho một app/env. Kiểm trước khi tạo.

    Đây là nửa mà `vault-onboard` cố ý chỉ IN ra: nó cần token quản trị Vault. Onboarding
    tự chạy được khi người chạy nó có token đó, và không làm gì cả khi mọi thứ đã có.
    """
    mount = _vault_str("auth_mount") or "kubernetes"
    role = vault_role_name(app, env)
    read_policy = vault_policy_name(app, env)
    write_policy = vault_policy_name(app, env, write=True)
    created = []

    for policy_name, write in ((read_policy, False), (write_policy, True)):
        status, _ = vault_api("GET", f"sys/policies/acl/{policy_name}", tolerate=(404,))
        if status == 404:
            vault_api("PUT", f"sys/policies/acl/{policy_name}",
                      {"policy": vault_policy(app, env, write=write)})
            created.append(f"policy {policy_name}")
        else:
            log(f"policy {policy_name} đã có -> giữ nguyên")

    status, _ = vault_api("GET", f"auth/{mount}/role/{role}", tolerate=(404,))
    if status == 404:
        vault_api("POST", f"auth/{mount}/role/{role}", {
            "bound_service_account_names": [vault_service_account(app, env)],
            "bound_service_account_namespaces": [app_namespace(app, env)],
            # CHỈ policy ĐỌC. VSO đăng nhập bằng role này; cấp thêm policy ghi ở đây là
            # cho operator quyền sửa mọi bí mật của app.
            "token_policies": [read_policy],
            "token_ttl": _vault_str("token_ttl") or "1h",
            "audience": _vault_str("auth_audience") or "vault",
        })
        created.append(f"role {role}")
    else:
        log(f"role {role} đã có -> giữ nguyên")
    return {"role": role, "readPolicy": read_policy, "writePolicy": write_policy,
            "created": created}


def ensure_vault_secret_keys(app: str, env: str, name: str, keys: dict[str, str]) -> list[str]:
    """Bảo đảm một secret có đủ các khoá, sinh giá trị cho khoá còn thiếu. Trả khoá đã tạo.

    `keys` là {tên khoá: giá trị hoặc "" để sinh ngẫu nhiên}. Khoá ĐÃ CÓ thì không đụng
    tới — ghi đè mật khẩu của một database đang chạy là cách chắc chắn nhất để app mất
    kết nối mà không ai hiểu vì sao.
    """
    mount = _vault_str("kv_mount") or "kv"
    kv_type = (_vault_str("kv_type") or "kv-v2").lower()
    rel = vault_relative_path(app, env, name)
    present = vault_secret_key_names(app, env, name)
    missing = [k for k in keys if present is None or k not in present]
    if not missing:
        log(f"{mount}/{rel} đã có đủ khoá {sorted(keys)} -> không ghi")
        return []

    alphabet = string.ascii_letters + string.digits
    payload = {}
    for key in missing:
        payload[key] = keys[key] or "".join(secrets.choice(alphabet) for _ in range(32))
    if kv_type == "kv-v2":
        path = f"{mount}/data/{rel}"
        body = {"data": payload}
    else:
        path = f"{mount}/{rel}"
        body = payload
    # PATCH khi secret đã tồn tại, POST khi chưa: patch không tạo được đường dẫn mới, còn
    # post lên một đường dẫn có sẵn sẽ XOÁ các khoá khác trong cùng secret.
    if present is None or kv_type != "kv-v2":
        vault_api("POST", path, body)
    else:
        # PATCH, không phải POST: secret này có thể đã mang khoá của người khác ghi trước
        # đó, và POST lên kv-v2 THAY THẾ toàn bộ phiên bản — tức xoá sạch những khoá kia.
        # kv-v2 chỉ chấp nhận patch với đúng Content-Type này.
        vault_api("PATCH", path, body,
                  content_type="application/merge-patch+json")
    log(f"đã ghi khoá {sorted(missing)} vào {mount}/{rel} — giá trị không được in ra")
    return missing


# ------------------------------------------------------------------- các bước (13.3)
@dataclass
class OnboardStep:
    key: str
    state: str
    fn: object
    doc: str


class OnboardContext:
    """Mọi thứ một bước cần, gom một chỗ, để bước nào cũng gọi được như nhau."""

    def __init__(self, request: dict, record: dict, store: OnboardingStore, args):
        self.request = request
        self.record = record
        self.store = store
        self.args = args
        self.app = request["application"]["name"]
        self.owner = request["application"]["owner"]
        self.catalog = Path(getattr(args, "catalog", None)
                            or REPO_ROOT)
        self.work = Path(getattr(args, "work", None) or f"onboard-{self.app}")
        self.kubeconfig = getattr(args, "kubeconfig", None)

    # ---- tiện ích dùng chung
    def save(self) -> None:
        self.record["updatedAt"] = onboarding_now()
        self.store.write(self.record)

    def out(self, key: str, value) -> None:
        """Ghi một kết quả vào state NGAY. Bước sau đọc nó; retry cũng đọc nó."""
        self.record.setdefault("outputs", {})[key] = value
        self.save()

    @property
    def outputs(self) -> dict:
        return self.record.setdefault("outputs", {})

    @property
    def app_dir(self) -> Path:
        return self.work / "app"

    def org(self) -> str:
        return str(CONFIG.require("git.org"))

    def app_repo(self) -> str:
        pattern = CONFIG.get("git.app_repo_pattern") or "{app}"
        return f"{self.org()}/{pattern.replace('{app}', self.app)}"

    def config_repo(self) -> str:
        pattern = CONFIG.get("git.config_repo_pattern") or "{app}-config"
        return f"{self.org()}/{pattern.replace('{app}', self.app)}"

    def wants(self, env: str) -> bool:
        return bool(self.request["environments"].get(env))


def github_repo_url(slug: str) -> str | None:
    cp = run(["gh", "repo", "view", slug, "--json", "url", "--jq", ".url"],
             check=False, capture=True)
    return cp.stdout.strip() or None if cp.returncode == 0 else None


def _git(ctx_dir: Path, *argv: str, check: bool = True):
    return run(["git", *argv], cwd=ctx_dir, check=check, capture=True)


def step_validate(ctx: OnboardContext) -> None:
    """1 + 2 của mục 13.3: kiểm mọi thứ có thể kiểm TRƯỚC khi tạo bất cứ thứ gì.

    Cờ tính năng kiểm ở đây chứ không phải ở lúc render: một lần onboarding tạo repo,
    namespace và credential rồi mới phát hiện platform chưa bật tính năng là để lại một
    đống rác mà không ai dọn.
    """
    require_onboarding_enabled()
    if not feature("stack_onboarding"):
        raise SystemExit(
            "features.stack_onboarding is off in platform.env.yaml. Onboarding sinh ra kho "
            "ứng dụng từ stack catalog, nên nó phải được bật trước — bật cho cả platform, "
            "một lần."
        )
    for name in ("application_values", "vault_secrets"):
        if not feature(name):
            raise SystemExit(
                f"features.{name} is off in platform.env.yaml. App sinh từ stack khai "
                "values theo môi trường và đọc bí mật qua Vault; thiếu cờ này thì lần "
                "render đầu tiên sẽ fail SAU khi repo và namespace đã được tạo."
            )
    if ctx.request["database"]["enabled"] and not feature("postgres_application"):
        raise SystemExit(
            "features.postgres_application is off in platform.env.yaml, nhưng stack này "
            "xin một database `class: application`."
        )

    # Toạ độ bắt buộc — hỏi SỚM, vì thiếu chúng thì bước 3 hoặc bước 8 mới hỏng.
    for key in ("git.org", "git.platform_repo", "registry.path"):
        CONFIG.require(key)

    for env in ENVIRONMENTS:
        if not ctx.wants(env):
            continue
        domain = CONFIG.get(f"environments.{env}.domain") or ""
        host = f"{ctx.app}.{domain}" if domain else ""
        if not host or len(host) > 253 or not re.match(
                r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", host):
            raise SystemExit(
                f"hostname suy ra cho {env} không hợp lệ: {host!r} "
                f"(environments.{env}.domain = {domain!r})."
            )
        ctx.outputs.setdefault("hostnames", {})[env] = host
    ctx.save()


def step_scaffold_repository(ctx: OnboardContext) -> None:
    """3 của mục 13.3: kho ứng dụng sinh từ stack, kèm CI của chính nó.

    Idempotent theo nghĩa mạnh: kho đã có mã nguồn thì KHÔNG sinh đè. Đội ứng dụng bắt đầu
    viết code ngay sau lần onboarding đầu tiên, và một lần retry không có quyền ghi đè
    việc của họ. `generate_stack` cũng không ghi đè từng file, nên hai lớp bảo vệ.
    """
    slug = ctx.app_repo()
    # Nhánh staging của kho ỨNG DỤNG dùng chung tên với nhánh staging của kho cấu hình.
    # Một giá trị, không hai: workflow CI sinh ra cũng nghe đúng tên này (xem
    # render_app_ci_workflow), nên đổi cấu hình là đổi cả hai đầu cùng lúc.
    branch = CONFIG.get("environments.staging.config_branch") or "dev"
    dest = ctx.app_dir
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _git(dest, "init", "-q", "-b", branch)

    url = github_repo_url(slug)
    if url:
        log(f"kho ứng dụng {slug} đã có -> không tạo lại")
    else:
        description = ctx.request["application"]["description"] or f"Ứng dụng {ctx.app}"
        run(["gh", "repo", "create", slug, "--private", "--description", description])
        url = github_repo_url(slug)
        if not url:
            raise SystemExit(f"tạo kho {slug} xong nhưng không đọc lại được — dừng ở đây.")
    ctx.out("appRepo", url)          # ghi NGAY: retry phải thấy kho này đã tồn tại

    _git(dest, "remote", "add", "origin", url)
    fetched = _git(dest, "fetch", "-q", "origin", branch, check=False).returncode == 0
    if fetched:
        _git(dest, "checkout", "-q", "-B", branch, f"origin/{branch}")
        if list(dest.glob("*/score.yaml")) or (dest / "score.yaml").is_file():
            log(f"{slug}@{branch} đã có mã nguồn -> chỉ bổ sung thứ còn thiếu")

    generate_stack(ctx.catalog, ctx.request["stack"]["id"], ctx.app, dest,
                   owner=ctx.owner)
    write_app_ci_workflow(dest, ctx.app, catalog=ctx.catalog)

    _git(dest, "config", "user.name", CONFIG.get("git.committer_name", "idp-orchestrator"))
    _git(dest, "config", "user.email",
         CONFIG.get("git.committer_email", "idp-orchestrator@noreply.invalid"))
    _git(dest, "add", "-A")
    if _git(dest, "diff", "--cached", "--quiet", check=False).returncode != 0:
        _git(dest, "commit", "-qm",
             f"chore(idp): dựng {ctx.app} từ stack {ctx.request['stack']['id']} "
             f"v{ctx.request['stack']['version']}")
        _git(dest, "push", "-q", "origin", f"HEAD:{branch}")
        log(f"đẩy scaffold lên {slug}@{branch}")
    else:
        log("không có gì mới để commit -> kho đã ở đúng hình dạng")

    # Nhánh production của kho ỨNG DỤNG. Gieo một lần rồi thôi: sau đó nó chỉ đổi qua
    # pull request của đội ứng dụng.
    prod_branch = CONFIG.get("git.default_branch", "main")
    if _git(dest, "ls-remote", "--exit-code", "--heads", "origin", prod_branch,
            check=False).returncode != 0:
        _git(dest, "push", "-q", "origin", f"HEAD:{prod_branch}")
        log(f"gieo nhánh {prod_branch} của kho ứng dụng")

    sha = _git(dest, "rev-parse", "HEAD").stdout.strip()
    ctx.out("sha", sha)


def ensure_app_checkout(ctx: OnboardContext, at_sha: str = "") -> Path:
    """Bản checkout kho ứng dụng, dựng lại từ remote nếu cần. Trả về thư mục.

    Vì sao không dựa vào thư mục mà bước scaffold để lại: một lần retry hiếm khi chạy trên
    cùng cái máy đã bỏ dở. `--work` mới tinh thì không có bản checkout nào cả, và mọi bước
    sau đều cần nó.

    Hai chế độ, và khác biệt giữa chúng là một lỗi thật đang chờ xảy ra:
      * `at_sha` rỗng — lấy ĐỈNH nhánh. Dùng ở bước build: giữa lúc onboarding bỏ dở và
        lúc chạy lại, đội ứng dụng thường đã đẩy code lên. Deploy commit cũ ở đây là âm
        thầm bỏ qua việc của họ.
      * `at_sha` có giá trị — checkout ĐÚNG commit đó. Dùng ở bước deploy/verify: manifest
        phải trỏ tới ảnh vừa build, và nếu ai đó đẩy commit mới ngay giữa hai bước thì
        render theo đỉnh nhánh sẽ sinh ra một tham chiếu ảnh chưa ai đẩy lên.
    """
    dest = ctx.app_dir
    branch = CONFIG.get("environments.staging.config_branch") or "dev"
    url = ctx.outputs.get("appRepo") or github_repo_url(ctx.app_repo())
    if not url:
        raise SystemExit(f"chưa có kho ứng dụng {ctx.app_repo()} — bước scaffold chưa chạy?")
    if dest.exists():
        shutil.rmtree(dest)
    run(["git", "clone", "-q", "--branch", branch, url, str(dest)])
    if at_sha:
        _git(dest, "checkout", "-q", at_sha)
        return dest
    sha = _git(dest, "rev-parse", "HEAD").stdout.strip()
    if ctx.outputs.get("sha") and sha != ctx.outputs["sha"]:
        log(f"kho ứng dụng đã có commit mới ({ctx.outputs['sha'][:8]} -> {sha[:8]}) "
            "-> onboarding đi tiếp với commit mới nhất")
    ctx.out("sha", sha)
    return dest


def step_bootstrap_platform(ctx: OnboardContext) -> None:
    """4 của mục 13.3: kho cấu hình, hai nhánh, khung Fleet, workflow verify.

    Bọc `tools/tao-app-moi.sh` thay vì viết lại: script đó đã idempotent, đã được dùng
    thật, và nó cố tình chạy bằng danh tính NGƯỜI DÙNG (quyền tạo repo). Onboarding chỉ
    cung cấp toạ độ cho nó — không cái nào gắn cứng trong script.
    """
    script = ctx.catalog / "tools" / "tao-app-moi.sh"
    if not script.is_file():
        raise SystemExit(f"không thấy {script}")
    org, _, config_repo = ctx.config_repo().partition("/")
    env = dict(os.environ)
    env.update({
        "ORG": org,
        "APP": ctx.app,
        "CONFIG_REPO": config_repo,
        "PLATFORM_REPO": str(CONFIG.require("git.platform_repo")),
        "NS_PATTERN": str(CONFIG.get("kubernetes.namespace_pattern") or "{app}-{env}"),
        "VERIFY_RUNNER_LABEL": json.dumps(CONFIG.get("ci.verify_runner_label"))
        if isinstance(CONFIG.get("ci.verify_runner_label"), list)
        else str(CONFIG.get("ci.verify_runner_label") or "ubuntu-latest"),
    })
    log(f"$ bash {script}   (ORG={org} APP={ctx.app} CONFIG_REPO={config_repo})")
    cp = subprocess.run(["bash", str(script)], env=env, text=True)
    if cp.returncode != 0:
        raise SystemExit(f"tao-app-moi.sh hỏng (mã {cp.returncode}) — xem log ở trên.")
    ctx.out("configRepo", github_repo_url(ctx.config_repo()) or ctx.config_repo())
    ensure_app_repo_secrets(ctx)


# Bí mật cấp KHO mà CI của app cần. Không phải bí mật của Vault: chúng sống trong GitHub,
# thuộc về CI, và không bao giờ được đọc bởi platform. Chỉ có TÊN nằm ở đây.
APP_REPO_SECRETS = {
    # Token để CI checkout kho platform (hỏi tên ảnh) và dispatch deploy-request.
    "PLATFORM_DISPATCH_TOKEN": "APP_DISPATCH_TOKEN",
}


def ensure_app_repo_secrets(ctx: OnboardContext) -> None:
    """Đặt bí mật cấp kho cho CI của app — nếu người chạy cung cấp giá trị.

    Vì sao không tự sinh: token này là danh tính, không phải một chuỗi ngẫu nhiên. Platform
    không có quyền tạo nó, và cấp cho onboarding quyền đúc token là đúng thứ mục 13.5 cấm.

    Vì sao vẫn nằm ở đây: thiếu nó thì lần push đầu tiên của đội ứng dụng ĐỎ, và thông báo
    lỗi nói về `actions/checkout` chứ không nói "chưa ai đặt secret". Bản ghi onboarding
    phải nói ra điều đó, kể cả khi nó không tự làm được.
    """
    slug = ctx.app_repo().split("/", 1)[-1] if "/" in ctx.app_repo() else ctx.app_repo()
    cp = run(["gh", "secret", "list", "-R", ctx.app_repo(), "--json", "name",
              "--jq", ".[].name"], check=False, capture=True)
    present = set(cp.stdout.split()) if cp.returncode == 0 else set()
    pending = []
    for name, env_var in sorted(APP_REPO_SECRETS.items()):
        if name in present:
            log(f"secret {name} của {slug} đã có -> giữ nguyên")
            continue
        value = os.environ.get(env_var)
        if not value:
            pending.append(name)
            continue
        # Giá trị đi qua stdin, không qua tham số: tham số dòng lệnh nằm trong `ps` của
        # mọi user khác trên máy.
        run(["gh", "secret", "set", name, "-R", ctx.app_repo()], stdin=value)
        log(f"đặt secret {name} cho {ctx.app_repo()} (giá trị không được in ra)")
    ctx.out("pendingRepoSecrets", pending)
    if pending:
        warn(f"{ctx.app_repo()} còn thiếu secret {', '.join(pending)} — CI của đội ứng "
             f"dụng sẽ đỏ ở bước checkout platform cho tới khi có người đặt chúng "
             f"(`gh secret set <tên> -R {ctx.app_repo()}`, hoặc chạy lại onboarding với "
             f"{'/'.join(APP_REPO_SECRETS[n] for n in pending)} trong môi trường).")


def step_configure_vault(ctx: OnboardContext) -> None:
    """5 của mục 13.3: namespace, ServiceAccount, VaultAuth, policy và role Vault.

    Chỉ cho staging ở giai đoạn này. Prod nhận đúng bộ này khi được kích hoạt — mục 13.3
    nói rõ: không dựng sẵn tài nguyên production cho một app chưa ai bật production.
    """
    ensure_onboarding_environment(ctx, "staging")


def ensure_onboarding_environment(ctx: OnboardContext, env: str) -> None:
    ns = app_namespace(ctx.app, env)
    ensure_namespace(ns, ctx.kubeconfig)
    labels = [f"{k}={v}" for k, v in onboarding_labels(ctx.request, env=env).items()]
    kubectl(["label", "namespace", ns, "--overwrite", *labels],
            kubeconfig=ctx.kubeconfig, check=False, capture=True)
    _emit(vault_auth_manifests(ctx.app, env),
          argparse.Namespace(apply=True, kubeconfig=ctx.kubeconfig))
    _emit(verify_rbac_manifests(ctx.app, env),
          argparse.Namespace(apply=True, kubeconfig=ctx.kubeconfig))
    access = ensure_vault_app_access(ctx.app, env)
    ctx.outputs.setdefault("vault", {})[env] = access
    ctx.save()


def database_workloads(app_dir) -> list[str]:
    """Workload nào xin `postgres` class `application` — đọc từ chính Score của app."""
    found = []
    for svc in discover(Path(app_dir)):
        spec = yaml.safe_load(svc.path.read_text()) or {}
        for res in (spec.get("resources") or {}).values():
            if isinstance(res, dict) and res.get("type") == "postgres" \
                    and res.get("class") == "application":
                found.append(svc.workload)
                break
    return sorted(found)


def database_username(workload: str) -> str:
    """Đúng quy tắc provisioner `postgres.application` dùng cho `.State.username`.

    Hai chỗ tính một tên nghĩa là có ngày chúng lệch nhau; ở đây hậu quả là CNPG tạo
    database với owner khác với user mà VSO đồng bộ vào Secret, và app nhận
    "password authentication failed" trên một credential nhìn thì đúng. Test ghim hai bên
    lại với nhau.
    """
    return "app_" + workload.replace("-", "_")


def step_provision_database(ctx: OnboardContext) -> None:
    """6 của mục 13.3: sinh credential database và ghi thẳng vào Vault.

    Phải xong TRƯỚC khi Fleet apply: CNPG đọc chính Secret do VSO đồng bộ để tạo user.
    Thiếu nó thì Cluster đứng ở bootstrap, còn app thì crash-loop vì không kết nối được —
    hai triệu chứng chẳng cái nào nhắc tới Vault.
    """
    ensure_database_credentials(ctx, "staging")


def ensure_database_credentials(ctx: OnboardContext, env: str) -> None:
    if not ctx.request["database"]["enabled"]:
        log("stack này không xin database -> bỏ qua")
        return
    # Bước này cũng phải chạy được trên một máy chưa từng thấy app: nó đọc Score của app
    # để biết workload nào xin database, và bản checkout có thể chưa tồn tại.
    workloads = database_workloads(
        ensure_app_checkout(ctx, at_sha=ctx.outputs.get("sha", "")))
    if not workloads:
        log("không thấy workload nào khai postgres class application -> bỏ qua")
        return
    if len(workloads) > 1:
        raise SystemExit(
            f"{ctx.app}: {len(workloads)} workload cùng khai `postgres class application` "
            f"({', '.join(workloads)}), nhưng provisioner đọc credential từ MỘT đường dẫn "
            f"Vault cho cả app ({_vault_str('kv_mount')}/apps/{ctx.app}/{env}/"
            f"{CONFIG.get('database.credential_secret')}). Hai database dùng chung một "
            "credential là một sự cố đang chờ xảy ra — tách app, hoặc mở rộng contract."
        )
    name = str(CONFIG.get("database.credential_secret") or "database")
    username = database_username(workloads[0])
    created = ensure_vault_secret_keys(ctx.app, env, name,
                                       {"username": username, "password": ""})
    ctx.outputs.setdefault("database", {})[env] = {
        "workload": workloads[0],
        "username": username,
        "vaultPath": f"{_vault_str('kv_mount') or 'kv'}/"
                     f"{vault_relative_path(ctx.app, env, name)}",
        "credentialCreated": bool(created),
    }
    ctx.save()


def image_exists(ref: str, *, attempts: int = 3) -> bool:
    """Ảnh đã có trên registry chưa. Hỏi lại vài lần trước khi kết luận là CHƯA.

    Cùng lý do mẫu CI hỏi ba lần: một lỗi mạng thoáng qua bị hiểu thành "chưa có ảnh" thì
    ta build lại một ảnh đã tồn tại — vô hại nhưng tốn, và che mất lỗi thật.
    """
    for attempt in range(1, attempts + 1):
        if run(["docker", "manifest", "inspect", ref],
               check=False, capture=True).returncode == 0:
            return True
        if attempt < attempts:
            time.sleep(3)
    return False


def step_build_images(ctx: OnboardContext) -> None:
    """7 của mục 13.3: ảnh cho commit vừa đẩy.

    Hai nguồn, cùng một KẾ HOẠCH: `--images ci` chờ CI của kho ứng dụng đẩy ảnh lên, còn
    `--images local` tự build. Cả hai đều dùng đúng context/Dockerfile mà `image-plan
    --with-build` trả về, nên không có đường nào để hai bên build khác nhau.
    """
    app_dir = ensure_app_checkout(ctx)
    services = discover(app_dir)
    registry = str(CONFIG.require("registry.path"))
    tag = ctx.outputs["sha"]
    plan = plan_images(services, registry, ctx.app, tag, app_dir,
                       resolve_tag_strategy(app_dir, ""))
    specs = build_specs(app_dir, services, ctx.catalog)
    mode = getattr(ctx.args, "images", "local")

    missing = {w: ref for w, ref in plan.items() if not image_exists(ref)}
    for workload, ref in sorted(missing.items()):
        if mode == "ci":
            raise OnboardingPaused(
                "PARTIALLY_READY",
                f"ảnh {ref} chưa có trên registry. CI của {ctx.app_repo()} build nó khi "
                f"commit {tag[:8]} được đẩy lên; chạy lại lệnh này khi CI xong.",
            )
        spec = specs[workload]
        log(f"build {workload}: {ref} (context {spec['context']}, {spec['dockerfile']})")
        run(["docker", "build", "-f", str(app_dir / spec["dockerfile"]),
             "-t", ref, str(app_dir / spec["context"])])
        run(["docker", "push", ref])
    if not missing:
        log("mọi ảnh đã có trên registry -> không build lại")
    ctx.out("images", plan)


def clone_config_repo(ctx: OnboardContext, env: str) -> Path:
    """Bản checkout của kho cấu hình ở đúng nhánh của môi trường này."""
    branch = CONFIG.get(f"environments.{env}.config_branch") \
        or CONFIG.get("git.default_branch", "main")
    dest = ctx.work / f"config-{env}"
    if dest.exists():
        shutil.rmtree(dest)
    url = ctx.outputs.get("configRepo") or github_repo_url(ctx.config_repo())
    if not url:
        raise SystemExit(f"chưa có kho cấu hình {ctx.config_repo()}")
    run(["git", "clone", "-q", "--branch", branch, url, str(dest)])
    return dest


def deploy_environment(ctx: OnboardContext, env: str) -> Path:
    """Render + apply-secrets + commit + GitRepo cho một môi trường. Trả config dir.

    Cố ý gọi thẳng các lệnh có sẵn thay vì viết lại: đường deploy phải là MỘT, dù nó được
    kích hoạt từ onboarding hay từ `repository_dispatch` của CI. Hai bản hiện thực là hai
    hành vi, và cái ít dùng hơn sẽ mục đi trong im lặng.
    """
    config_dir = clone_config_repo(ctx, env)
    sha = ctx.outputs["sha"]
    ensure_app_checkout(ctx, at_sha=sha)
    work = ctx.work / f"render-{env}"
    render_args = argparse.Namespace(
        app=ctx.app, image=ctx.app, tag=sha, registry=str(CONFIG.require("registry.path")),
        tag_strategy="", env=env, catalog=str(ctx.catalog), app_dir=str(ctx.app_dir),
        work=str(work), out=str(config_dir / env / "manifests.yaml"),
        kubeconfig=ctx.kubeconfig, state_file=getattr(ctx.args, "render_state_file", None),
        no_state=False,
    )
    cmd_render(render_args)
    cmd_apply_secrets(argparse.Namespace(
        app=ctx.app, env=env, secrets=str(work / "secrets.yaml"),
        harbor_host=os.environ.get("REGISTRY_HOST") or CONFIG.get("registry.host"),
        harbor_user=os.environ.get("REGISTRY_USER"),
        harbor_pass=os.environ.get("REGISTRY_PASS"),
        backup_key_id=os.environ.get("BACKUP_ACCESS_KEY_ID"),
        backup_secret_key=os.environ.get("BACKUP_ACCESS_SECRET_KEY"),
        kubeconfig=ctx.kubeconfig))
    return config_dir


def step_deploy_staging(ctx: OnboardContext) -> None:
    """8 + 9 của mục 13.3: render staging, ghi vào kho cấu hình, để Fleet kéo về."""
    config_dir = deploy_environment(ctx, "staging")
    cmd_commit(argparse.Namespace(
        config_dir=str(config_dir), app=ctx.app, env="staging",
        sha=ctx.outputs["sha"], app_dir=str(ctx.app_dir),
        catalog_ref=None, branch=None, via_pr=False))
    cmd_ensure_gitrepo(argparse.Namespace(
        app=ctx.app, env="staging", config_dir=str(config_dir),
        kubeconfig=ctx.kubeconfig, work=str(ctx.work)))
    ctx.out("manifests", {"staging": f"{ctx.outputs.get('configRepo')}"
                                     f"/blob/{CONFIG.get('environments.staging.config_branch')}"
                                     f"/staging/manifests.yaml"})


def third_party_secret_requirements(app_dir, app: str, env: str) -> list[dict]:
    """Bí mật mà NGƯỜI DÙNG phải tự nạp: mọi `secretRef` trong values của app.

    Credential database không nằm ở đây — platform tự sinh nó (bước 6). Phân biệt hai
    loại là điều làm nên khác nhau giữa "app đang chờ bạn dán API key" và "platform hỏng".
    """
    spec = load_application_values(Path(app_dir))
    if not spec:
        return []
    resolved = resolve_application_values(spec, env)
    wanted: dict[str, set[str]] = {}
    for key, value in resolved.items():
        if isinstance(value, dict) and "secretRef" in value:
            ref = value["secretRef"]
            wanted.setdefault(str(ref["name"]), set()).add(str(ref["key"]))
    out = []
    for name in sorted(wanted):
        present = vault_secret_key_names(app, env, name)
        missing = sorted(k for k in wanted[name] if present is None or k not in present)
        if missing:
            out.append({"secret": name, "keys": missing,
                        "path": f"{_vault_str('kv_mount') or 'kv'}/"
                                f"{vault_relative_path(app, env, name)}"})
    return out


def step_verify_staging(ctx: OnboardContext) -> None:
    """10 + 11 của mục 13.3: kiểm bí mật TRƯỚC, rồi mới chờ cụm.

    Thứ tự đó là toàn bộ điểm của WAITING_FOR_USER_SECRETS. Nếu chỉ chạy `verify`, một app
    thiếu API key sẽ chờ hết `initial_sync_timeout_seconds` rồi FAIL — đúng về mặt kỹ
    thuật, và hoàn toàn sai về mặt thông tin: không có gì hỏng, chỉ là chưa ai dán khoá vào.
    """
    app_dir = ensure_app_checkout(ctx, at_sha=ctx.outputs["sha"])
    missing = third_party_secret_requirements(app_dir, ctx.app, "staging")
    ctx.out("missingSecrets", missing)
    if missing:
        lines = [f"  idpctl secret-set --app {ctx.app} --env staging "
                 f"--name {item['secret']} --key {key}"
                 for item in missing for key in item["keys"]]
        raise OnboardingPaused(
            "WAITING_FOR_USER_SECRETS",
            f"{ctx.app}/staging đã được deploy nhưng còn thiếu bí mật của bên thứ ba. "
            "Nạp chúng rồi chạy lại đúng lệnh onboarding này:\n" + "\n".join(lines),
        )
    config_dir = ctx.work / "config-staging"
    manifests = config_dir / "staging" / "manifests.yaml"
    if not manifests.is_file():
        raise SystemExit(f"không thấy {manifests} — bước deploy chưa chạy xong?")
    cmd_verify(argparse.Namespace(
        app=ctx.app, env="staging", manifests=str(manifests),
        kubeconfig=ctx.kubeconfig,
        timeout=config_int("onboarding.verify_timeout_seconds", 420)))
    urls = sorted({h for doc in load_all(manifests)
                   if doc.get("kind") == "HTTPRoute"
                   for h in (doc.get("spec") or {}).get("hostnames") or []})
    scheme = str(CONFIG.get("ingress.route_scheme") or "http")
    ctx.out("stagingUrls", [f"{scheme}://{h}" for h in urls])


def step_staging_ready(ctx: OnboardContext) -> None:
    """Bước cuối của nửa staging: nói ra trạng thái đúng, không nói READY."""
    state = "PENDING_PROD_ACTIVATION" if ctx.wants("prod") else "STAGING_READY"
    record_state(ctx.record, state, ctx.store)


ONBOARD_PLAN = [
    OnboardStep("validate", "VALIDATING", step_validate,
                "kiểm tên app, chủ sở hữu, stack version, hostname và quyền"),
    OnboardStep("scaffold-repository", "SCAFFOLDING_REPOSITORY", step_scaffold_repository,
                "kho ứng dụng từ stack, kèm .github/workflows/ci.yaml"),
    OnboardStep("bootstrap-platform", "BOOTSTRAPPING_PLATFORM", step_bootstrap_platform,
                "kho cấu hình, hai nhánh, khung Fleet, workflow verify"),
    OnboardStep("configure-vault", "CONFIGURING_VAULT", step_configure_vault,
                "namespace, ServiceAccount, VaultAuth, policy và role Vault"),
    OnboardStep("provision-database", "PROVISIONING_DATABASE", step_provision_database,
                "credential database sinh thẳng vào Vault"),
    OnboardStep("build-images", "BUILDING_IMAGES", step_build_images,
                "ảnh cho commit vừa đẩy"),
    OnboardStep("deploy-staging", "DEPLOYING_STAGING", step_deploy_staging,
                "render staging và ghi vào kho cấu hình cho Fleet"),
    OnboardStep("verify-staging", "VERIFYING_STAGING", step_verify_staging,
                "bí mật, database, rollout và route"),
    OnboardStep("staging-ready", "STAGING_READY", step_staging_ready,
                "chốt trạng thái staging"),
]


# --------------------------------------------------------------------- nửa production
def step_provision_prod(ctx: OnboardContext) -> None:
    """12a: tài nguyên prod chỉ được tạo KHI có người kích hoạt, không phải lúc tạo app."""
    if not ctx.wants("prod"):
        raise SystemExit(
            f"request của {ctx.app} khai environments.prod: false. Kích hoạt production là "
            "một thay đổi của request, không phải một cờ dòng lệnh."
        )
    if (ctx.record.get("steps") or {}).get("verify-staging", {}).get("status") != "done":
        raise SystemExit(
            f"{ctx.app} chưa qua VERIFYING_STAGING. Prod chạy ĐÚNG bộ ảnh mà staging đã "
            "được kiểm — chưa kiểm thì không có gì để đưa lên."
        )
    ensure_onboarding_environment(ctx, "prod")
    ensure_database_credentials(ctx, "prod")


def step_deploy_prod(ctx: OnboardContext) -> None:
    """12b: render prod, ÉP dùng ảnh của staging, rồi mở pull request.

    Hai điều được ghim ở đây, và cả hai đều là gate của phase:

    * `copy_images` chạy SAU render. Render prod tính lại tên ảnh từ `--tag`, và với
      `tagStrategy: content` mỗi workload có nhãn riêng — nên "prod dùng ảnh đã verify"
      không phải là một giá trị mà là cả một bộ. Chép từ manifest staging là cách duy
      nhất đúng khi repo có nhiều service.
    * `via_pr=True` không điều kiện. Nhánh prod của kho cấu hình CÓ THỂ chưa bật bảo vệ —
      trên một cụm thử thì gần như chắc chắn là chưa — và nếu để logic đoán, prod sẽ được
      push thẳng. Đây là chỗ con người phải nhìn thấy diff.
    """
    config_dir = clone_config_repo(ctx, "prod")
    staging_branch = CONFIG.get("environments.staging.config_branch") or "dev"
    staging_manifests = ctx.work / "staging-manifests.yaml"
    cp = run(["git", "show", f"origin/{staging_branch}:staging/manifests.yaml"],
             cwd=config_dir, check=False, capture=True)
    if cp.returncode != 0:
        raise SystemExit(
            f"không đọc được staging/manifests.yaml trên nhánh {staging_branch} của kho "
            "cấu hình — prod không có nguồn ảnh nào để sao chép."
        )
    staging_manifests.write_text(cp.stdout)

    deploy_environment(ctx, "prod")
    prod_manifests = config_dir / "prod" / "manifests.yaml"
    moved = copy_images(staging_manifests, prod_manifests, ctx.app)
    log(f"prod lấy {moved} ảnh từ staging" if moved
        else "prod đã chạy đúng bộ ảnh của staging")
    ctx.out("prodImages", {
        d["metadata"]["name"]: [c.get("image") for c in
                                d["spec"]["template"]["spec"]["containers"]]
        for d in load_all(prod_manifests) if d.get("kind") == "Deployment"})

    url = cmd_commit(argparse.Namespace(
        config_dir=str(config_dir), app=ctx.app, env="prod", sha=ctx.outputs["sha"],
        app_dir=str(ctx.app_dir), catalog_ref=None, branch=None, via_pr=True))
    if url:
        ctx.out("prodPullRequest", url)
        raise OnboardingPaused(
            "PENDING_PROD_APPROVAL",
            f"prod của {ctx.app} đang chờ duyệt: {url}\n"
            "Merge pull request đó rồi chạy lại lệnh này để platform kiểm cụm prod.",
        )
    log("nhánh prod đã mang đúng manifest này -> pull request trước đó đã được merge")


def step_verify_prod(ctx: OnboardContext) -> None:
    config_dir = ctx.work / "config-prod"
    if not (config_dir / "prod" / "manifests.yaml").is_file():
        config_dir = clone_config_repo(ctx, "prod")
    manifests = config_dir / "prod" / "manifests.yaml"
    if not manifests.is_file():
        raise OnboardingPaused(
            "PENDING_PROD_APPROVAL",
            "nhánh prod của kho cấu hình chưa có prod/manifests.yaml — pull request chưa "
            "được merge. Không có gì để kiểm.",
        )
    missing = third_party_secret_requirements(ctx.app_dir, ctx.app, "prod")
    ctx.out("missingSecretsProd", missing)
    if missing:
        lines = [f"  idpctl secret-set --app {ctx.app} --env prod "
                 f"--name {item['secret']} --key {key}"
                 for item in missing for key in item["keys"]]
        raise OnboardingPaused(
            "WAITING_FOR_USER_SECRETS",
            "prod còn thiếu bí mật của bên thứ ba. Bí mật KHÔNG được sao chép từ staging "
            "sang prod — đó là chủ ý:\n" + "\n".join(lines))
    cmd_ensure_gitrepo(argparse.Namespace(
        app=ctx.app, env="prod", config_dir=str(config_dir),
        kubeconfig=ctx.kubeconfig, work=str(ctx.work)))
    cmd_verify(argparse.Namespace(
        app=ctx.app, env="prod", manifests=str(manifests), kubeconfig=ctx.kubeconfig,
        timeout=config_int("onboarding.verify_timeout_seconds", 420)))
    urls = sorted({h for doc in load_all(manifests) if doc.get("kind") == "HTTPRoute"
                   for h in (doc.get("spec") or {}).get("hostnames") or []})
    scheme = str(CONFIG.get("ingress.route_scheme") or "http")
    ctx.out("prodUrls", [f"{scheme}://{h}" for h in urls])


def step_prod_ready(ctx: OnboardContext) -> None:
    record_state(ctx.record, "READY", ctx.store)


ONBOARD_PROD_PLAN = [
    OnboardStep("provision-prod", "PROVISIONING_PROD", step_provision_prod,
                "namespace, Vault và credential database của prod"),
    OnboardStep("deploy-prod", "PENDING_PROD_APPROVAL", step_deploy_prod,
                "render prod bằng ảnh của staging, mở pull request"),
    OnboardStep("verify-prod", "VERIFYING_PROD", step_verify_prod,
                "Fleet, bí mật, database và rollout của prod"),
    OnboardStep("prod-ready", "READY", step_prod_ready, "chốt trạng thái"),
]


# --------------------------------------------------------------------------- engine
def run_onboarding(ctx: OnboardContext, plan=None, *, stop_after: str = "") -> dict:
    """Chạy các bước theo thứ tự, bỏ qua bước đã xong, dừng có trạng thái khi phải chờ.

    Ba tính chất, mỗi cái đều là một gate của phase:
      * bước đã `done` thì KHÔNG chạy lại — retry không sinh bản sao;
      * `OnboardingPaused` là dừng có trạng thái, không phải lỗi — WAITING_FOR_USER_SECRETS
        và PENDING_PROD_APPROVAL đều không bao giờ trở thành READY;
      * bước hỏng ghi FAILED_RETRYABLE kèm lý do rồi ném tiếp — lần chạy sau bắt đầu lại
        đúng từ bước đó, chứ không phải từ đầu.
    """
    forced = set(getattr(ctx.args, "force_step", None) or [])
    unknown = sorted(forced - {s.key for s in (plan or ONBOARD_PLAN)})
    if unknown:
        raise SystemExit(f"--force-step: không có bước nào tên {', '.join(unknown)}")
    for step in (plan or ONBOARD_PLAN):
        status = (ctx.record.get("steps") or {}).get(step.key, {}).get("status")
        if status == "done" and step.key not in forced:
            log(f"bước {step.key} đã xong -> bỏ qua")
            continue
        record_state(ctx.record, step.state, ctx.store)
        log(f"==> [{step.state}] {step.key}: {step.doc}")
        try:
            step.fn(ctx)
        except OnboardingPaused as paused:
            ctx.record.setdefault("steps", {})[step.key] = {
                "status": "waiting", "at": onboarding_now(), "note": paused.message}
            record_state(ctx.record, paused.state, ctx.store)
            warn(paused.message)
            return ctx.record
        except SystemExit as exc:
            ctx.record.setdefault("steps", {})[step.key] = {
                "status": "failed", "at": onboarding_now(), "error": str(exc)}
            record_state(ctx.record, "FAILED_RETRYABLE", ctx.store)
            raise
        ctx.record.setdefault("steps", {})[step.key] = {
            "status": "done", "at": onboarding_now()}
        ctx.save()
        if stop_after and stop_after == step.key:
            log(f"dừng sau bước {step.key} theo yêu cầu")
            break
    return ctx.record


def onboarding_summary(record: dict) -> str:
    """11 của mục 13.3: trả về đúng thứ người vừa tạo app cần đọc."""
    out = [f"onboarding {record.get('requestId')}: {record.get('state')}"]
    for step in ONBOARD_PLAN + ONBOARD_PROD_PLAN:
        info = (record.get("steps") or {}).get(step.key)
        if info:
            mark = {"done": "OK ", "waiting": "CHỜ", "failed": "HỎNG"}.get(
                info.get("status"), "?")
            out.append(f"  [{mark}] {step.key}")
    outputs = record.get("outputs") or {}
    for label, key in (("kho ứng dụng", "appRepo"), ("kho cấu hình", "configRepo"),
                       ("pull request prod", "prodPullRequest")):
        if outputs.get(key):
            out.append(f"  {label}: {outputs[key]}")
    for label, key in (("staging", "stagingUrls"), ("prod", "prodUrls")):
        for url in outputs.get(key) or []:
            out.append(f"  {label}: {url}")
    for env, info in sorted((outputs.get("database") or {}).items()):
        out.append(f"  database {env}: user {info['username']} "
                   f"(credential ở {info['vaultPath']})")
    for item in outputs.get("missingSecrets") or []:
        out.append(f"  CÒN THIẾU: {item['path']} khoá {', '.join(item['keys'])}")
    return "\n".join(out)


def cmd_onboard(args) -> None:
    # Gate before even loading the request or creating its state ConfigMap. A disabled
    # command must have zero external side effects, including when resuming an old record.
    require_onboarding_enabled()
    request = load_onboarding_request(args.request, getattr(args, "catalog", None))
    store = make_onboarding_store(request["application"]["name"], args)
    record = load_or_create_record(store, request)
    ctx = OnboardContext(request, record, store, args)
    ctx.work.mkdir(parents=True, exist_ok=True)
    run_onboarding(ctx, stop_after=getattr(args, "stop_after", "") or "")
    print(onboarding_summary(record))


def _record_context(args) -> OnboardContext:
    store = make_onboarding_store(args.app, args)
    record = store.read()
    if record is None:
        raise SystemExit(
            f"không có bản ghi onboarding nào cho {args.app!r}. Chạy `onboard --request` "
            "trước, hoặc kiểm lại --state-file/--kubeconfig đang trỏ đúng chỗ."
        )
    return OnboardContext(record["request"], record, store, args)


def cmd_onboard_status(args) -> None:
    ctx = _record_context(args)
    if getattr(args, "json", False):
        print(json.dumps(ctx.record, indent=2, sort_keys=True, ensure_ascii=False))
        return
    print(onboarding_summary(ctx.record))


def cmd_onboard_activate_prod(args) -> None:
    """12 của mục 13.3. Lệnh RIÊNG, không phải một cờ của `onboard` — và đó là chủ ý:
    đưa một app lên production là một quyết định, không phải một bước tiếp theo."""
    # Production activation is part of onboarding too; disabling only the initial command
    # would leave an existing STAGING_READY record able to mutate production.
    require_onboarding_enabled()
    ctx = _record_context(args)
    allowed = ("STAGING_READY", "PENDING_PROD_ACTIVATION", "PROVISIONING_PROD",
               "PENDING_PROD_APPROVAL", "VERIFYING_PROD", "READY",
               "WAITING_FOR_USER_SECRETS", "FAILED_RETRYABLE")
    if ctx.record.get("state") not in allowed:
        raise SystemExit(
            f"{ctx.app} đang ở trạng thái {ctx.record.get('state')} — staging chưa xong. "
            "Chạy `onboard` cho tới STAGING_READY trước."
        )
    ctx.work.mkdir(parents=True, exist_ok=True)
    # Prod render từ ĐÚNG commit staging đã được verify, không phải từ đỉnh nhánh.
    ensure_app_checkout(ctx, at_sha=ctx.record["outputs"]["sha"])
    run_onboarding(ctx, ONBOARD_PROD_PLAN, stop_after=getattr(args, "stop_after", "") or "")
    print(onboarding_summary(ctx.record))


# --------------------------------------------------------------------------------------
# offboard — xoá một app (mục 13.4)
# --------------------------------------------------------------------------------------
# Ba tính chất mà mục 13.4 đòi, và vì sao từng cái tồn tại:
#
#   PREVIEW    — mặc định lệnh này KHÔNG xoá gì. Nó in ra chính xác những gì sẽ bị xoá và
#                những gì sẽ được GIỮ LẠI kèm lý do. Một lệnh xoá mà phải chạy mới biết nó
#                làm gì là một lệnh không ai dám chạy ở prod, nên rốt cuộc người ta xoá tay
#                — và xoá tay mới là thứ xoá nhầm.
#   APPROVAL   — `--execute` đòi gõ lại đúng tên app; `prod` đòi thêm `--approved-by`, và
#                tên đó được ghi vào bản ghi state để sau này còn tra được ai đã duyệt.
#   LIFECYCLE  — không phải cái gì cũng xoá cùng lúc. Backup của database KHÔNG bị đụng
#                tới (retention của kho object lo việc đó); bí mật trong Vault mặc định chỉ
#                xoá mềm; kho Git thì không xoá bao giờ.
#
# Và tính chất thứ tư, thứ khiến ba cái trên đáng tin: KHÔNG XOÁ NHẦM CỦA ĐỘI KHÁC. Mọi
# tài nguyên phải TỰ CHỨNG MINH nó thuộc app này trước khi bị chạm tới. Suy từ tên là
# không đủ — `{app}-{env}` là một quy ước, không phải một bằng chứng.
def offboard_targets(app: str, env: str, kubeconfig) -> tuple[list[dict], list[dict]]:
    """Trả về (sẽ xoá, sẽ giữ). Không gọi lệnh xoá nào."""
    remove: list[dict] = []
    keep: list[dict] = []
    ns = app_namespace(app, env)

    cp = kubectl(["get", "namespace", ns, "-o", "json"],
                 kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode == 0:
        # BẰNG CHỨNG, KHÔNG PHẢI TÊN. Namespace do platform tạo không mang nhãn, nên tên
        # đúng quy ước là tất cả những gì ta có ở mức namespace — và nó KHÔNG đủ. Nên ta
        # hỏi thứ bên trong: nếu có bất kỳ tài nguyên nào tự khai thuộc một application
        # KHÁC, thì đây là namespace dùng chung và ta không được xoá nó.
        others = sorted(_foreign_applications(ns, app, kubeconfig))
        if others:
            keep.append({"kind": "Namespace", "name": ns,
                         "why": f"có tài nguyên của application khác: {', '.join(others)}"})
        else:
            remove.append({"kind": "Namespace", "name": ns,
                           "why": "chứa toàn bộ workload/Cluster/Secret của app này"})
    else:
        keep.append({"kind": "Namespace", "name": ns, "why": "không tồn tại"})

    # GitRepo của Fleet: LIỆT-KÊ-RỒI-KHỚP theo `spec.repo`, không đoán theo tên. Đoán tên
    # là cách nhanh nhất để xoá GitRepo của một đội đặt tên trùng quy ước.
    fleet_ns = CONFIG.get("kubernetes.fleet_namespace", "fleet-local") or "fleet-local"
    want = onboarding_config_repo_url(app)
    cp = kubectl(["get", "gitrepo", "-n", fleet_ns, "-o", "json"],
                 kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode == 0:
        for obj in (json.loads(cp.stdout or "{}").get("items") or []):
            spec, meta = obj.get("spec") or {}, obj.get("metadata") or {}
            same = re.sub(r"\.git$", "", spec.get("repo", "")) == re.sub(r"\.git$", "", want)
            entry = {"kind": "GitRepo", "name": f"{fleet_ns}/{meta.get('name')}",
                     "why": f"trỏ vào {spec.get('repo')}"}
            if same and env in (spec.get("paths") or [env]):
                remove.append(entry)
            elif same:
                keep.append({**entry, "why": f"cùng kho nhưng paths={spec.get('paths')} "
                                             f"— không phải môi trường {env}"})

    # Bí mật trong Vault: đúng tiền tố apps/<app>/<env>/, tức đúng ranh giới mà policy của
    # app đã vẽ ra. Không quét rộng hơn, vì rộng hơn là chạm sang app khác.
    remove.append({"kind": "VaultPrefix",
                   "name": f"{CONFIG.get('vault.kv_mount') or 'kv'}/"
                           f"{vault_prefix_for(app, env)}",
                   "why": "tiền tố bí mật của riêng app/env này"})

    # Những thứ CỐ Ý không xoá.
    keep.append({"kind": "DatabaseBackup", "name": CONFIG.get("database.backup.object_store_url") or "(chưa cấu hình)",
                 "why": "xoá app không được xoá đường phục hồi; retention của kho object "
                        "quyết định, không phải lệnh này"})
    keep.append({"kind": "GitRepository", "name": want,
                 "why": "kho Git giữ lịch sử triển khai; hãy archive, đừng xoá"})
    keep.append({"kind": "VaultPolicy",
                 "name": (CONFIG.get("vault.policy_template") or "idp-{application}-{environment}")
                         .replace("{application}", app).replace("{environment}", env),
                 "why": "policy/role Vault do Vault Ops sở hữu — gỡ bằng quy trình của họ"})
    return remove, keep


def _foreign_applications(ns: str, app: str, kubeconfig) -> set[str]:
    """Nhãn `idp.platform/application` khác `app` xuất hiện trong namespace này."""
    found: set[str] = set()
    kinds = "deploy,statefulset,cronjob,job,service,configmap,secret,pvc"
    cp = kubectl(["get", kinds, "-n", ns, "-o", "json"],
                 kubeconfig=kubeconfig, check=False, capture=True)
    if cp.returncode != 0:
        return found
    for obj in (json.loads(cp.stdout or "{}").get("items") or []):
        owner = ((obj.get("metadata") or {}).get("labels") or {}).get("idp.platform/application")
        if owner and owner != app:
            found.add(owner)
    return found


def git_server_url() -> str:
    """Base URL of the Git server, from the environment GitHub Actions already sets.

    `GITHUB_SERVER_URL` is `https://github.com` on github.com and the GHES base URL on an
    Enterprise runner — the same variable the orchestrator workflow already relies on for
    auth. Hardcoding github.com here is not just a cosmetic URL: this value is compared
    against a Fleet GitRepo's `spec.repo` during offboard, and on GHES that repo URL is the
    Enterprise host, so a github.com literal would silently match nothing.
    """
    return (os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")


def onboarding_config_repo_url(app: str) -> str:
    org = CONFIG.get("git.org") or ""
    pattern = CONFIG.get("git.config_repo_pattern") or "{app}-config"
    return f"{git_server_url()}/{org}/{pattern.replace('{app}', app)}"


def cmd_offboard(args) -> None:
    app, env = validate_secret_name(args.app), validate_environment(args.env)
    remove, keep = offboard_targets(app, env, args.kubeconfig)

    print(f"\n=== KẾ HOẠCH XOÁ {app}/{env} ===\n")
    print("SẼ XOÁ:")
    for t in remove:
        print(f"  - {t['kind']:14} {t['name']}\n      vì: {t['why']}")
    print("\nSẼ GIỮ:")
    for t in keep:
        print(f"  - {t['kind']:14} {t['name']}\n      vì: {t['why']}")
    blocked = [t for t in keep if t["kind"] == "Namespace" and "application khác" in t["why"]]
    print()

    store = make_onboarding_store(app, args)
    record = store.read()

    if not args.execute:
        print("Đây là BẢN XEM TRƯỚC — chưa có gì bị xoá.")
        print(f"Chạy thật:  offboard --app {app} --env {env} --execute --confirm {app}"
              + (" --approved-by <tên>" if env == "prod" else ""))
        if record is not None:
            record["state"] = "DELETE_PLANNED"
            record.setdefault("history", []).append(
                {"at": onboarding_now(), "state": "DELETE_PLANNED", "env": env})
            store.write(record)
        return

    # ---- từ đây là hành động thật ----
    if args.confirm != app:
        raise SystemExit(
            f"--confirm phải là đúng tên app ({app!r}), nhận được {args.confirm!r}. "
            "Gõ lại tên là rào chắn cuối cùng trước một thao tác không hoàn tác được.")
    if env == "prod" and not args.approved_by:
        raise SystemExit(
            "xoá ở prod đòi --approved-by <tên người duyệt>. Tên đó được ghi vào bản ghi "
            "state, nên sau này còn tra được ai đã đồng ý.")
    if blocked:
        raise SystemExit(
            f"namespace {app_namespace(app, env)} có tài nguyên của application khác "
            f"({blocked[0]['why']}). Từ chối xoá: xoá app này sẽ kéo theo của đội khác.")

    if record is not None:
        record["state"] = "DELETING"
        record.setdefault("deletion", {}).update(
            {"env": env, "approvedBy": args.approved_by or "", "at": onboarding_now(),
             "purgeSecrets": bool(args.purge_secrets)})
        record.setdefault("history", []).append(
            {"at": onboarding_now(), "state": "DELETING", "env": env})
        store.write(record)

    done = []
    for t in remove:
        if t["kind"] == "GitRepo":
            ns_, name_ = t["name"].split("/", 1)
            kubectl(["delete", "gitrepo", name_, "-n", ns_, "--ignore-not-found"],
                    kubeconfig=args.kubeconfig, check=True, capture=True)
        elif t["kind"] == "Namespace":
            # Sau GitRepo, luôn luôn. Xoá namespace trước thì Fleet thấy bundle thiếu tài
            # nguyên và dựng lại tất cả trong lúc ta đang xoá — hai bên đánh nhau, và
            # Fleet thắng.
            kubectl(["delete", "namespace", t["name"], "--ignore-not-found", "--wait=false"],
                    kubeconfig=args.kubeconfig, check=True, capture=True)
        elif t["kind"] == "VaultPrefix":
            _offboard_vault(app, env, purge=args.purge_secrets)
        done.append(f"{t['kind']}:{t['name']}")
        log(f"đã xoá {t['kind']} {t['name']}")

    if record is not None:
        record["state"] = "DELETED"
        record["deletion"]["removed"] = done
        record.setdefault("history", []).append(
            {"at": onboarding_now(), "state": "DELETED", "env": env})
        store.write(record)
    print(f"\n{app}/{env}: đã xoá {len(done)} nhóm tài nguyên. Kho Git và backup còn nguyên.")


def _offboard_vault(app: str, env: str, *, purge: bool) -> None:
    """Xoá bí mật của app/env. Mặc định XOÁ MỀM.

    kv-v2 phân biệt `data` (xoá mềm, phục hồi được) với `metadata` (xoá hẳn, không lấy
    lại được). Mặc định mềm là có chủ ý: xoá nhầm một app rồi phát hiện sau vài giờ là
    chuyện có thật, và khi đó thứ khó dựng lại nhất chính là bí mật — mọi thứ khác đều
    render lại được từ Git.
    """
    address = (os.environ.get("VAULT_ADDR") or "").rstrip("/")
    token = os.environ.get("VAULT_TOKEN")
    if not address or not token:
        warn("bỏ qua phần Vault: chưa đặt VAULT_ADDR/VAULT_TOKEN. Bí mật của app VẪN CÒN.")
        return
    mount = CONFIG.get("vault.kv_mount") or "kv"
    prefix = vault_prefix_for(app, env)
    headers = {"X-Vault-Token": token}
    if CONFIG.get("vault.namespace"):
        headers["X-Vault-Namespace"] = CONFIG.get("vault.namespace")

    def call(method: str, url: str):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers, method=method),
                    timeout=30) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            raise SystemExit(f"Vault từ chối {method} {url} ({exc.code})") from None
        except urllib.error.URLError as exc:
            raise SystemExit(f"không tới được Vault: {exc.reason}") from None

    listed = call("LIST", f"{address}/v1/{mount}/metadata/{prefix}")
    names = ((listed.get("data") or {}).get("keys") or [])
    for name in names:
        leaf = f"{prefix}/{name}".rstrip("/")
        kind = "metadata" if purge else "data"
        call("DELETE", f"{address}/v1/{mount}/{kind}/{leaf}")
        log(f"{'xoá hẳn' if purge else 'xoá mềm'} {mount}/{leaf}")
    if not names:
        log(f"không có bí mật nào dưới {mount}/{prefix}")
