#!/usr/bin/env python3
"""Command-line interface for idpctl. Business logic lives in sibling modules."""
from __future__ import annotations

from . import context as _context
from . import resources as _resources
from . import catalog as _catalog
from . import render as _render
from . import delivery as _delivery
from . import onboarding as _onboarding
for _module in (_context, _resources, _catalog, _render, _delivery, _onboarding):
    globals().update({n: getattr(_module, n) for n in dir(_module) if not n.startswith("__")})
def cmd_config(args) -> None:
    """Expose platform.env.yaml to the workflow, so the YAML holds no infrastructure value.

    `--export` prints KEY=value lines the workflow appends to $GITHUB_ENV. The one thing
    that CANNOT come from here is `runs-on`: GitHub resolves it before any step executes,
    so runner labels have to be a repository variable. That is still configuration rather
    than code, but it is a second place to edit and the docs must say so.
    """
    if args.get:
        value = CONFIG.get(args.get)
        if value is None:
            raise SystemExit(f"no such key in platform.env.yaml: {args.get}")
        # Danh sách in ra dạng JSON. JSON là YAML hợp lệ, nên ["a","b"] dán thẳng vào
        # `runs-on:` được — dùng cho nhãn máy chạy gồm nhiều nhãn. In str(list) của Python
        # sẽ ra dấu nháy đơn, YAML đọc được nhưng trông lạ và dễ bị sửa nhầm.
        print(json.dumps(value) if isinstance(value, list) else value)
        return
    if args.export:
        table = CONFIG.for_env(args.env)
        for key in ("git.org", "registry.host", "registry.path", "registry.pull_secret",
                    "kubernetes.state_namespace", "ingress.gateway_name"):
            if key in table:
                print(f"{key.replace('.', '_').upper()}={table[key]}")
        pattern = CONFIG.get("git.config_repo_pattern", "{app}-config")
        print(f"CONFIG_REPO_PATTERN={pattern}")
        return
    print(json.dumps(CONFIG.data, indent=2, sort_keys=True))


def cmd_image_plan(args) -> None:
    """Print {workload: image ref} as JSON, for an app's CI to build against.

    The naming rule lives here and nowhere else. An app's CI asks what to build instead of
    reimplementing the rule, because a mismatch between the two is invisible until Fleet
    applies a manifest referencing an image that was never pushed.

    `--with-build` adds the build recipe (context + Dockerfile) and therefore changes the
    JSON shape. It is a NEW flag rather than a new default because every app already
    running has a copy of the old CI template that reads `.[workload]` as a string; making
    the richer shape the default would break all of them at once, on the next push.
    """
    app_dir = Path(args.app_dir)
    services = discover(app_dir)
    plan = plan_images(services, args.registry, args.image, args.tag, app_dir,
                       resolve_tag_strategy(app_dir, args.tag_strategy))
    if getattr(args, "with_build", False):
        specs = build_specs(app_dir, services, getattr(args, "catalog", None))
        plan = {w: dict(specs[w], image=ref) for w, ref in plan.items()}
    print(json.dumps(plan, indent=2, sort_keys=True))

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Global: every subcommand may need an infrastructure value, and none of them should
    # ever have one baked in.
    ap.add_argument("--env-config", help="path to platform.env.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_state_flags(p):
        p.add_argument("--state-file", help="persist state in this file instead of a cluster Secret")
        p.add_argument("--no-state", action="store_true",
                       help="disable state persistence (reproduces the churn bug; tests only)")

    def add_render_flags(p, *, paths_required: bool):
        p.add_argument("--app", required=True)
        p.add_argument("--image", help="Harbor image name; defaults to --app")
        p.add_argument("--tag", required=True, help="image tag, normally the commit SHA")
        # No default: the registry is infrastructure, so it comes from platform.env.yaml
        # via the workflow. A hardcoded fallback here is exactly how a deploy ends up
        # pushing to the wrong company's registry.
        p.add_argument("--registry", required=True)
        # Default is EMPTY, not "content": empty means "nobody said", which lets
        # `.idp/stack.yaml` have an opinion (see resolve_tag_strategy). An app with no stack
        # file still lands on "content", so nothing already deployed changes.
        p.add_argument(
            "--tag-strategy", choices=("", "commit", "content"), default="",
            help="content: mỗi workload mang mã băm THƯ MỤC của chính nó. commit: mọi "
                 "workload dùng --tag. Bỏ trống: đọc .idp/stack.yaml, mặc định content.",
        )
        # Optional for `promote --mode tag-only`, which rewrites an existing manifest and
        # needs no catalog, app checkout or scratch dir.
        # Xác nhận tường minh rằng database mới được phép RỖNG khi đổi class. Không có
        # mặc định "cứ chạy đi": xem check_postgres_class_migration.
        p.add_argument("--accept-empty-database", action="store_true",
                       help="khi đổi `class` của một postgres đã có state: chấp nhận dựng "
                            "database mới RỖNG và bỏ lại dữ liệu cũ trên PVC cũ")
        p.add_argument("--catalog", required=paths_required, help="checkout of the idp catalog")
        p.add_argument("--app-dir", required=paths_required, help="checkout of the app repo")
        p.add_argument("--work", required=paths_required, help="scratch dir for this render")
        p.add_argument("--kubeconfig")
        add_state_flags(p)

    p = sub.add_parser("verify", help="chờ cụm thực sự chạy đúng thứ vừa render")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--manifests", required=True)
    p.add_argument("--kubeconfig")
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("ensure-gitrepo",
                       help="tạo GitRepo của Fleet nếu chưa có (không bao giờ ghi đè)")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--config-dir", required=True)
    p.add_argument("--kubeconfig")
    p.add_argument("--work")
    p.set_defaults(func=cmd_ensure_gitrepo)

    p = sub.add_parser("config", help="read platform.env.yaml (for the workflow to consume)")
    p.add_argument("--get", help="dotted key, e.g. registry.path")
    p.add_argument("--export", action="store_true",
                   help="print KEY=value lines for $GITHUB_ENV")
    p.add_argument("--env", default="staging")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("image-plan", help="print the workload -> image map this app renders to")
    p.add_argument("--app", required=True)
    p.add_argument("--image", help="image name; defaults to --app")
    p.add_argument("--tag", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--app-dir", required=True)
    p.add_argument("--tag-strategy", choices=("", "commit", "content"), default="")
    p.add_argument("--with-build", action="store_true",
                   help="kèm context và Dockerfile của từng workload (đổi hình dạng JSON)")
    p.add_argument("--catalog", default=str(REPO_ROOT),
                   help="checkout của kho platform, để đọc buildContext của stack")
    p.set_defaults(func=cmd_image_plan)

    # ---- stack catalog. `--catalog` defaults to this file's own checkout: that is where
    # the templates live, and it is code location, not an infrastructure value.
    def add_catalog_flag(p):
        p.add_argument("--catalog", default=str(REPO_ROOT),
                       help="checkout của kho platform (nơi có templates/stacks/)")

    p = sub.add_parser("stack-list", help="các stack mà catalog này phát hành")
    add_catalog_flag(p)
    p.set_defaults(func=cmd_stack_list)

    p = sub.add_parser("stack-new", help="dựng một kho ứng dụng mới từ một stack")
    p.add_argument("--stack", required=True, help="id của stack, vd node-fullstack")
    p.add_argument("--app", required=True)
    p.add_argument("--out", required=True, help="thư mục kho ứng dụng sẽ được dựng vào")
    p.add_argument("--owner", default="", help="đội sở hữu, ghi vào .idp/stack.yaml")
    p.add_argument("--catalog-ref", default="",
                   help="ref catalog để ghim vào platform.lock của app mới")
    p.add_argument("--force", action="store_true",
                   help="ghi đè file đã có (mặc định: giữ nguyên, nên chạy lại được)")
    add_catalog_flag(p)
    p.set_defaults(func=cmd_stack_new)

    p = sub.add_parser("stack-validate",
                       help="kho ứng dụng còn khớp với stack nó khai không")
    p.add_argument("--app-dir", required=True)
    add_catalog_flag(p)
    p.set_defaults(func=cmd_stack_validate)

    p = sub.add_parser("stack-upgrade",
                       help="in diff giữa kho ứng dụng và phiên bản stack hiện tại")
    p.add_argument("--app-dir", required=True)
    p.add_argument("--app", default="", help="ghi đè metadata.application nếu file thiếu")
    p.add_argument("--write", action="store_true",
                   help="ghi thay đổi vào working tree (vẫn phải tự mở pull request)")
    p.add_argument("--all", action="store_true",
                   help="diff cả mã nguồn, không chỉ file do platform sở hữu")
    p.add_argument("--work", default="", help="thư mục tạm để render bản mới")
    add_catalog_flag(p)
    p.set_defaults(func=cmd_stack_upgrade)

    p = sub.add_parser("preflight")
    p.add_argument("--require-cluster", action="store_true")
    p.add_argument("--require-score-compose", action="store_true",
                   help="also demand score-compose at its pinned version (stack CI, local dev)")
    p.add_argument("--require-vault", action="store_true",
                   help="also demand VSO at its pinned version plus VaultConnection/VaultAuthGlobal")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("doctor",
                       help="kiểm capability cụm khớp config theo feature/backend (read-only)")
    p.add_argument("--kubeconfig")
    p.add_argument("--no-cluster", action="store_true",
                   help="chỉ kiểm config, không truy cập cụm")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("vault-foundation",
                       help="in VaultConnection + VaultAuthGlobal (một bộ cho mỗi cụm)")
    p.add_argument("--apply", action="store_true", help="kubectl apply thay vì chỉ in ra")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_vault_foundation)

    p = sub.add_parser("vault-onboard",
                       help="ServiceAccount + VaultAuth cho một app/env, kèm policy và role Vault")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--print-policy", action="store_true", help="chỉ in HCL của policy")
    p.add_argument("--write", action="store_true",
                   help="với --print-policy: in policy GHI (dành cho người/onboarding, không cho VSO)")
    p.add_argument("--apply", action="store_true", help="kubectl apply phần Kubernetes")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_vault_onboard)

    # `idp-secret set` trong kế hoạch. Dành cho NGƯỜI, không cho CI: nó cần Vault token có
    # policy ghi. Giá trị chỉ vào qua nhập ẩn hoặc stdin — không có cờ --value, vì tham số
    # dòng lệnh nằm trong history và trong `ps` của mọi user khác trên máy.
    p = sub.add_parser("secret-set",
                       help="ghi một khoá bí mật vào Vault ở đúng đường dẫn platform suy ra")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--name", required=True, help="tên secret logic, vd: stripe")
    p.add_argument("--key", required=True, help="khoá bên trong secret đó, vd: api_key")
    p.add_argument("--stdin", action="store_true", help="đọc giá trị từ stdin thay vì nhập ẩn")
    p.add_argument("--generate", action="store_true",
                   help="sinh ngẫu nhiên (dùng cho credential do PLATFORM sở hữu, vd mật "
                        "khẩu database). Giá trị không bao giờ được in ra.")
    p.add_argument("--replace", action="store_true",
                   help="ghi đè toàn bộ secret (mặc định chỉ vá đúng khoá này)")
    p.set_defaults(func=cmd_secret_set)

    p = sub.add_parser("offboard",
                       help="xoá một app: mặc định chỉ XEM TRƯỚC (mục 13.4)")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--execute", action="store_true",
                   help="thật sự xoá. Không có cờ này thì chỉ in kế hoạch.")
    p.add_argument("--confirm", default="",
                   help="gõ lại đúng tên app; bắt buộc khi có --execute")
    p.add_argument("--approved-by", default="",
                   help="ai duyệt việc xoá này; bắt buộc ở prod, ghi vào bản ghi state")
    p.add_argument("--purge-secrets", action="store_true",
                   help="xoá HẲN bí mật trong Vault thay vì xoá mềm (không lấy lại được)")
    p.add_argument("--state-file",
                   help="đọc/ghi bản ghi onboarding trong file thay vì ConfigMap")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_offboard)

    p = sub.add_parser("rotate-db-credential",
                       help="xoay vòng mật khẩu database theo đúng thứ tự Vault -> VSO -> "
                            "CNPG -> pod, kiểm từng bước")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_rotate_db_credential)

    p = sub.add_parser("verify-rbac",
                       help="in danh tính chỉ-đọc dùng để verify (không có quyền đọc Secret)")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_verify_rbac)

    # ---- onboarding (Phase 6). `--work` giữ nguyên giữa các lần chạy là CÓ Ý: bản
    # checkout kho ứng dụng và kho cấu hình nằm ở đó, và lần retry dùng lại chúng.
    def add_resume_flags(p):
        p.add_argument("--stop-after", default="",
                       help="dừng sau bước này (xem tên bước trong `onboard-status`)")
        p.add_argument("--force-step", action="append", default=[],
                       help="chạy lại một bước đã `done` (mọi bước đều kiểm-trước-khi-"
                            "tạo, nên chạy lại là an toàn). Lặp cờ này cho nhiều bước.")

    def add_onboard_state_flags(p):
        p.add_argument("--state-file",
                       help="giữ bản ghi onboarding trong file thay vì ConfigMap trong cụm")
        p.add_argument("--kubeconfig")

    p = sub.add_parser("onboard",
                       help="chạy máy trạng thái onboarding cho một request (mục 13)")
    p.add_argument("--request", required=True, help="file YAML theo mục 13.1")
    p.add_argument("--work", help="thư mục làm việc, mặc định onboard-<app>")
    p.add_argument("--images", choices=("local", "ci"), default="local",
                   help="local: tự build và đẩy ảnh; ci: chờ CI của kho ứng dụng đẩy")
    add_resume_flags(p)
    add_catalog_flag(p)
    add_onboard_state_flags(p)
    p.set_defaults(func=cmd_onboard)

    p = sub.add_parser("onboard-status", help="trạng thái và kết quả của một lần onboarding")
    p.add_argument("--app", required=True)
    p.add_argument("--json", action="store_true", help="in nguyên bản ghi")
    add_onboard_state_flags(p)
    p.set_defaults(func=cmd_onboard_status)

    p = sub.add_parser("onboard-activate-prod",
                       help="kích hoạt production: dùng ảnh đã verify ở staging, qua pull request")
    p.add_argument("--app", required=True)
    p.add_argument("--work", help="thư mục làm việc của lần onboarding trước")
    add_resume_flags(p)
    add_catalog_flag(p)
    add_onboard_state_flags(p)
    p.set_defaults(func=cmd_onboard_activate_prod)

    p = sub.add_parser("render")
    add_render_flags(p, paths_required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("apply-secrets")
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--secrets", required=True)
    p.add_argument("--harbor-host")
    p.add_argument("--harbor-user")
    p.add_argument("--backup-key-id", help="ACCESS_KEY_ID của kho object (mặc định "
                                           "BACKUP_ACCESS_KEY_ID)")
    p.add_argument("--backup-secret-key", help="ACCESS_SECRET_KEY của kho object (mặc định "
                                               "BACKUP_ACCESS_SECRET_KEY)")
    p.add_argument("--harbor-pass")
    p.add_argument("--kubeconfig")
    p.set_defaults(func=cmd_apply_secrets)

    p = sub.add_parser("commit")
    p.add_argument("--config-dir", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--env", required=True, choices=("staging", "prod"))
    p.add_argument("--sha", required=True)
    p.add_argument("--app-dir", help="app checkout, needed for the ancestry guard")
    p.add_argument("--catalog-ref")
    p.add_argument("--branch", help="branch of the config repo this environment targets")
    p.add_argument("--via-pr", action="store_true",
                   help="open a pull request instead of pushing; for environments that "
                        "require review before the cluster changes")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("promote")
    add_render_flags(p, paths_required=False)
    p.add_argument("--mode", required=True,
                   choices=("from-staging", "tag-only", "re-render"))
    p.add_argument("--config-dir", required=True)
    p.set_defaults(func=cmd_promote)

    args = ap.parse_args(argv)
    set_config(EnvConfig.load(args.env_config))
    if getattr(args, "image", None) is None and hasattr(args, "app"):
        args.image = args.app
    args.func(args)
