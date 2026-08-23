"""Git, GitHub, Fleet, promotion and runtime deployment verification."""
from __future__ import annotations

from . import context as _context
from . import resources as _resources
from . import render as _render
for _module in (_context, _resources, _render):
    globals().update({n: getattr(_module, n) for n in dir(_module) if not n.startswith("__")})


def repo_is_private(url: str) -> bool | None:
    """Kho có riêng tư không? None nghĩa là không hỏi được.

    Dùng để phân biệt hai trường hợp trông giống nhau khi thiếu credential: kho công khai
    thì Fleet clone ẩn danh vẫn được, kho riêng tư thì chắc chắn hỏng.
    """
    m = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        return None
    cp = run(["gh", "api", f"repos/{m.group(1)}/{m.group(2)}", "--jq", ".private"],
             check=False, capture=True)
    return {"true": True, "false": False}.get(cp.stdout.strip())


def cmd_ensure_gitrepo(args) -> None:
    """Tạo GitRepo của Fleet nếu chưa có. KHÔNG BAO GIỜ ghi đè cái đang có.

    Đây là lỗi im lặng số một của cả hệ thống: quên tạo GitRepo thì orchestrator xanh
    toàn tập, manifest nằm đúng trong repo cấu hình, mà cụm trống trơn — vì không ai kéo
    về. Tự tạo ở đây thì khỏi phải nhớ.

    Vì sao không ghi đè: cụm thường đã có GitRepo của đội khác. Đặt trùng tên rồi apply
    là ĐÈ LÊN họ, và ứng dụng của họ ngừng đồng bộ trong im lặng. Nên: thiếu thì tạo,
    có rồi mà trỏ đúng kho thì để yên, trỏ kho KHÁC thì dừng và báo.
    """
    name = f"{args.app}-{args.env}"
    ns = CONFIG.get("kubernetes.fleet_namespace", "fleet-local") or "fleet-local"
    branch = CONFIG.get(f"environments.{args.env}.config_branch") \
        or CONFIG.get("git.default_branch", "main")

    # Địa chỉ kho lấy từ chính bản checkout, không dựng lại từ mẫu tên — dựng lại là
    # thêm một chỗ có thể lệch với thực tế.
    url = run(["git", "remote", "get-url", "origin"],
              cwd=Path(args.config_dir), capture=True).stdout.strip()
    url = re.sub(r"^https://[^@]+@", "https://", url)      # bỏ token nếu remote có nhúng
    url = re.sub(r"\.git$", "", url)

    cp = kubectl(["get", "gitrepo", name, "-n", ns, "-o", "json"],
                 kubeconfig=args.kubeconfig, check=False, capture=True)
    if cp.returncode == 0:
        cur = (json.loads(cp.stdout).get("spec") or {}).get("repo", "")
        if re.sub(r"\.git$", "", cur) == url:
            log(f"GitRepo {ns}/{name} đã có, trỏ đúng {url} -> giữ nguyên")
            return
        raise SystemExit(
            f"GitRepo {ns}/{name} đã tồn tại nhưng trỏ tới '{cur}', không phải '{url}'. "
            "Nó có thể là của ứng dụng khác — apply đè lên sẽ làm ứng dụng đó ngừng đồng "
            "bộ mà không báo gì. Đổi tên app, hoặc xoá GitRepo cũ nếu chắc chắn nó thừa."
        )

    # Thông tin đăng nhập git cho Fleet: KHÔNG áp đặt một tên mặc định.
    # Khai một tên secret không tồn tại thì Fleet không clone được — mà lỗi đó nằm trong
    # status của GitRepo, không ai nhìn, và triệu chứng lại y hệt "quên tạo GitRepo".
    # Thứ tự: lấy từ cấu hình nếu có khai; không thì HỌC THEO các GitRepo đang chạy trên
    # cùng namespace; không có gì để học thì bỏ trống, để Fleet tự xoay như nó vẫn làm với
    # kho công khai hoặc secret mặc định của cụm.
    secret = CONFIG.get("kubernetes.fleet_git_secret") or ""

    # Đã có ai đăng ký chỗ này dưới TÊN KHÁC chưa? Bản cài cũ thường đặt tên không kèm
    # môi trường (ví dụ `demo` thay vì `demo-staging`). Tạo thêm một cái nữa thì hai
    # GitRepo cùng đồng bộ một thư mục, sinh hai Bundle chồng nhau — không hỏng ngay
    # nhưng rối và rất khó truy khi cần gỡ.
    cp = kubectl(["get", "gitrepo", "-n", ns, "-o", "json"],
                 kubeconfig=args.kubeconfig, check=False, capture=True)
    if cp.returncode == 0:
        hang_xom = []
        for item in (json.loads(cp.stdout).get("items") or []):
            spec = item.get("spec") or {}
            if (re.sub(r"\.git$", "", spec.get("repo", "")) == url
                    and args.env in (spec.get("paths") or [])):
                log(f"kho này đã được đăng ký dưới tên {item['metadata']['name']} "
                    f"-> không tạo thêm {name}")
                return
            if spec.get("clientSecretName"):
                hang_xom.append((item["metadata"]["name"], spec["clientSecretName"]))
        if not secret and hang_xom:
            ten, secret = hang_xom[0]
            log(f"học theo GitRepo {ten}: dùng clientSecretName={secret}")

    body = {
        "apiVersion": "fleet.cattle.io/v1alpha1",
        "kind": "GitRepo",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"repo": url, "branch": branch, "paths": [args.env],
                 "pollingInterval": "15s"},
    }
    if secret:
        body["spec"]["clientSecretName"] = secret
    elif repo_is_private(url):
        # Kho riêng tư + không có credential = Fleet clone ẩn danh và hỏng CHẮC CHẮN với
        # "authentication required: Anonymous access denied". Nhưng nó hỏng ở status của
        # GitRepo, không ai nhìn, và triệu chứng y hệt "quên tạo GitRepo": cụm trống trơn.
        # Dừng ngay ở đây thì lỗi hiện ra đúng chỗ, đúng lúc, kèm cách sửa.
        raise SystemExit(
            f"kho cấu hình {url} là kho RIÊNG TƯ nhưng không tìm được thông tin đăng nhập "
            "git nào cho Fleet.\n"
            "Fleet sẽ clone ẩn danh và hỏng với 'Anonymous access denied' — cụm trống trơn "
            "trong khi mọi bước ở đây báo xanh.\n\n"
            "Cách sửa: tạo secret trong namespace của Fleet rồi khai tên nó vào "
            "kubernetes.fleet_git_secret:\n"
            "  kubectl -n <fleet-ns> create secret generic git-creds-idp \\\n"
            "    --type=kubernetes.io/basic-auth \\\n"
            "    --from-literal=username=<tài-khoản> --from-literal=password=<token>\n"
            "Lưu ý kiểu secret phải khớp địa chỉ kho: https -> basic-auth, ssh -> ssh-auth."
        )
    else:
        log(f"{url} là kho công khai và không có clientSecretName -> Fleet clone ẩn danh")
    tmp = Path(args.work or ".") / f"gitrepo-{name}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(body))
    _tolerate_exists(
        kubectl(["create", "-f", str(tmp)], kubeconfig=args.kubeconfig,
                check=False, capture=True),
        f"GitRepo {ns}/{name} -> {url} nhánh {branch} thư mục {args.env}",
    )
def is_ancestor(app_dir: Path, maybe_ancestor: str, descendant: str) -> bool | None:
    """True/False, or None when git cannot tell (shallow clone, unknown commit)."""
    cp = run(["git", "merge-base", "--is-ancestor", maybe_ancestor, descendant],
             cwd=app_dir, check=False, capture=True)
    if cp.returncode == 0:
        return True
    if cp.returncode == 1:
        return False
    return None


class OutOfOrder(SystemExit):
    """The commit being deployed is older than what is already deployed."""


def guard_ordering(deployed: str, sha: str, app_dir: Path | None, env: str) -> None:
    """Refuse to move `env` backwards.

    Build durations differ, so a later commit can dispatch BEFORE an earlier one: the
    concurrency group serializes runs but does not reorder them. Without this, the older
    render simply wins and the environment silently regresses.

    `deployed` is whatever the config repo currently records — read it from the version we
    are actually about to write on top of, not from a checkout taken minutes ago.
    """
    if not deployed or not app_dir:
        return
    if deployed == sha:
        log(f"{env} already at {sha}")
        return
    anc = is_ancestor(app_dir, sha, deployed)
    if anc is True:
        raise OutOfOrder(
            f"refusing to deploy {sha} to {env}: it is an ancestor of the already-deployed "
            f"{deployed} (out-of-order dispatch)"
        )
    if anc is None:
        warn(
            f"cannot determine ancestry between {sha} and {deployed} — the app checkout is "
            "probably shallow (needs fetch-depth: 0), or the ref is a tag that was never "
            "fetched. Proceeding."
        )


def upstream_record(config: Path, env: str, base: str) -> str:
    """The deploy record as it exists on the remote RIGHT NOW ('' if absent).

    Read after a fetch, so it reflects writers that landed since this job cloned the repo.
    `base` is the branch this deploy targets — with the PR flow the working branch is a
    throwaway, so reading HEAD's name would compare against the wrong thing.
    """
    cp = run(["git", "show", f"origin/{base}:{sha_record_dir()}/{env}.sha"],
             cwd=config, check=False, capture=True)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def branch_is_protected(config: Path, branch: str) -> bool | None:
    """Nhánh đích có branch protection không? None nghĩa là không xác định được.

    Đây là NGUỒN SỰ THẬT DUY NHẤT cho việc "môi trường này có cần duyệt không". Trước đây
    nó là một cờ trong platform.env.yaml, tức là hai nơi cùng khai một sự thật — và một cờ
    ghi `require_pr: false` trong khi nhánh thật đang được bảo vệ là một lời nói dối chỉ
    vỡ ra lúc push.

    Hệ quả thực tế: một repo demo không bật bảo vệ thì tự phục vụ hoàn toàn, không phải
    khai gì. Đến khi đội đó làm nghiêm túc, họ bật protection và platform tự chuyển sang
    chế độ pull request — không sửa cấu hình, không deploy lại.
    """
    url = run(["git", "remote", "get-url", "origin"],
              cwd=config, check=False, capture=True).stdout.strip()
    m = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        return None
    cp = run(["gh", "api", f"repos/{m.group(1)}/{m.group(2)}/branches/{branch}",
              "--jq", ".protected"], cwd=config, check=False, capture=True)
    if cp.returncode != 0:
        return None
    value = cp.stdout.strip()
    return {"true": True, "false": False}.get(value)


def open_pull_request(config: Path, base: str, head: str, title: str, body: str) -> str:
    """Open a PR and return its URL. Does NOT merge — a human approves and merges.

    Used for environments whose branch requires review. Deliberately stops here: the point
    of the approval is that a person looks at the manifest diff before production changes,
    and a bot that merges its own PR would defeat it.
    """
    cp = run(["gh", "pr", "create", "--base", base, "--head", head,
              "--title", title, "--body", body],
             cwd=config, check=False, capture=True)
    if cp.returncode != 0:
        err = (cp.stderr or "") + (cp.stdout or "")
        # A PR for this branch may already exist if the job is being re-run.
        if "already exists" in err:
            existing = run(["gh", "pr", "view", head, "--json", "url", "-q", ".url"],
                           cwd=config, check=False, capture=True)
            if existing.returncode == 0:
                return existing.stdout.strip()
        raise SystemExit(f"could not open pull request: {err.strip()}")
    return cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else ""


def cmd_commit(args) -> None:
    config, app_dir = Path(args.config_dir), Path(args.app_dir) if args.app_dir else None
    record = config / sha_record_dir() / f"{args.env}.sha"

    if record.is_file():
        guard_ordering(record.read_text().strip(), args.sha, app_dir, args.env)

    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(args.sha + "\n")

    # Branch and review requirement come from platform.env.yaml, read HERE rather than
    # compared as strings in the workflow YAML. `require_pr: "true"` written as a string
    # instead of a boolean would silently fail a YAML string comparison and push straight
    # at a protected branch; Python decides once, correctly, for every caller.
    # Base is ALWAYS the branch actually checked out — we must push to the thing we
    # rendered against, never to a branch named somewhere else. The configured value is
    # used to CHECK that, not to override it: if they disagree the job cloned one branch
    # and would publish to another, which is how an environment silently gets the wrong
    # manifests. Fail instead.
    checked_out = run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                      cwd=config, capture=True).stdout.strip()
    base = getattr(args, "branch", None) or checked_out
    configured = CONFIG.get(f"environments.{args.env}.config_branch")
    if configured and configured != base:
        raise SystemExit(
            f"config repo is checked out on '{base}' but platform.env.yaml says {args.env} "
            f"lives on '{configured}'. The checkout and the target must match, or this "
            "deploy would publish manifests to a branch it never rendered against."
        )
    # Hỏi GitHub, không đọc cấu hình. Xem branch_is_protected().
    protected = branch_is_protected(config, base)
    if protected is None:
        # Không xác định được thì đi đường push thẳng — CỐ Ý.
        # Nếu nhánh thật ra có bảo vệ, GitHub sẽ từ chối push kèm thông báo rõ ràng
        # (GH006), tức là hỏng ỒN ÀO. Còn đoán ngược lại thì sinh ra một pull request
        # nằm im không ai biết, trên một repo demo chẳng ai chờ pull request nào cả.
        # Việc cưỡng chế nằm ở phía GitHub, không phải ở đoán của chúng ta.
        warn(f"không xác định được nhánh {base} có bảo vệ hay không -> thử push thẳng")
    via_pr = getattr(args, "via_pr", False) or bool(protected)
    log(f"{base}: bảo vệ={protected} -> {'mở pull request' if via_pr else 'push thẳng'}")

    # Danh tính này quyết định lịch sử config repo ghi công cho ai — thứ dùng để truy vết
    # "ai deploy cái gì". Lấy từ cấu hình, không gắn cứng.
    run(["git", "config", "user.name",
         CONFIG.get("git.committer_name", "idp-orchestrator")], cwd=config)
    run(["git", "config", "user.email",
         CONFIG.get("git.committer_email", "idp-orchestrator@noreply.invalid")], cwd=config)
    run(["git", "add", "."], cwd=config)
    nothing_staged = run(["git", "diff", "--cached", "--quiet"],
                         cwd=config, check=False).returncode == 0

    # "Nothing new to stage" is NOT the same as "nothing to do". A previous attempt may have
    # committed and then failed to push; returning here would leave that commit stranded and
    # report success, so re-running a broken deploy would fix nothing. Check for unpushed
    # work before giving up.
    run(["git", "fetch", "origin", base], cwd=config, check=False)
    unpushed = run(["git", "rev-list", "--count", f"origin/{base}..HEAD"],
                   cwd=config, check=False, capture=True)
    ahead = unpushed.returncode == 0 and unpushed.stdout.strip() not in ("", "0")

    if nothing_staged and not ahead:
        log("no manifest changes")
        return None
    if not nothing_staged:
        msg = f"deploy({args.app}): {args.env} {args.sha}"
        if args.catalog_ref:
            msg += f" (catalog: {args.catalog_ref})"
        run(["git", "commit", "-m", msg], cwd=config)
    else:
        msg = f"deploy({args.app}): {args.env} {args.sha}"
        log(f"nothing new to commit, but {unpushed.stdout.strip()} commit(s) never reached "
            f"origin/{base} -> pushing those")

    # Environments whose branch requires review never get a direct push. The bot puts the
    # change on its own branch and opens a PR; a person reads the manifest diff and merges.
    if via_pr:
        head = f"deploy/{args.app}-{args.env}-{args.sha[:8]}"
        run(["git", "checkout", "-B", head], cwd=config)
        run(["git", "push", "--force-with-lease", "origin", head], cwd=config)
        url = open_pull_request(
            config, base, head,
            title=msg,
            body=(
                f"Triển khai tự động do orchestrator sinh ra.\n\n"
                f"| | |\n|---|---|\n"
                f"| app | `{args.app}` |\n| môi trường | `{args.env}` |\n"
                f"| commit | `{args.sha}` |\n| catalog | `{args.catalog_ref or 'n/a'}` |\n\n"
                "Diff bên dưới chính là thứ sẽ thay đổi trên cụm sau khi merge.\n"
                "**Không sửa tay** — lần triển khai sau sẽ ghi đè."
            ),
        )
        log(f"opened pull request into {base}: {url}")
        print(url)
        # Trả về, không chỉ in: máy trạng thái onboarding cần chính URL này để ghi vào
        # state và dừng ở PENDING_PROD_APPROVAL. Đọc lại stdout của chính mình là cách
        # nhanh nhất để hai bên lệch nhau khi có ai đó thêm một dòng log.
        return url

    # Push EXPLICITLY to the branch we validated, never a bare `git push`. A bare push
    # depends on tracking configuration: a branch checked out without an upstream fails
    # with "no upstream branch", which the retry below then misreads as "somebody pushed
    # first" and sends into a rebase that cannot work. Naming the target also removes any
    # chance of publishing to whatever branch tracking happens to point at.
    for attempt in (1, 2, 3):
        if run(["git", "push", "origin", f"HEAD:{base}"],
               cwd=config, check=False).returncode == 0:
            log(f"pushed to {base}")
            return None
        if attempt == 3:
            raise SystemExit("push failed after 3 attempts")
        warn(f"push rejected (new commits upstream) -> pull --rebase, retry ({attempt}/3)")

        # RE-CHECK BEFORE REBASING. Somebody landed commits since this job cloned, and a
        # rebase replays OUR commit on top of theirs — including our deploy record. If what
        # they pushed is newer than what we are holding, rebasing would quietly roll the
        # environment back. The guard only ran against the stale clone, so run it again
        # against what is actually on the remote now.
        run(["git", "fetch", "origin"], cwd=config)
        guard_ordering(upstream_record(config, args.env, base), args.sha, app_dir, args.env)

        rebase = run(["git", "pull", "--rebase", "origin", base],
                     cwd=config, check=False, capture=True)
        if rebase.returncode != 0:
            # Usually a genuine conflict, or a branch with no upstream. Either way the
            # retry loop cannot make progress, so fail with the reason rather than a traceback.
            run(["git", "rebase", "--abort"], cwd=config, check=False)
            raise SystemExit(
                "cannot rebase onto the config repo: "
                f"{(rebase.stderr or rebase.stdout or '').strip()}"
            )


# --------------------------------------------------------------------------------------
# promote
# --------------------------------------------------------------------------------------
def replace_tag(ref: str, tag: str) -> str:
    """Swap the tag, treating ':' as a separator only after the last '/' so that a registry
    port (harbor:5000/x/y) is never mistaken for a tag."""
    slash, colon = ref.rfind("/"), ref.rfind(":")
    return (ref[:colon] if colon > slash else ref) + ":" + tag


def retag(path: Path, image: str, tag: str) -> int:
    """Retag only this app's own images; datastore images (postgres:17-alpine) stay put.
    Matches <image>: for a single-workload app and <image>- for multi-workload."""
    docs = load_all(path)
    pattern = re.compile(rf"/{re.escape(image)}[:-]")
    changed = 0
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        spec = doc.get("spec", {}).get("template", {}).get("spec", {})
        for container in spec.get("containers", []) or []:
            ref = container.get("image", "")
            if pattern.search(ref):
                container["image"] = replace_tag(ref, tag)
                changed += 1
    dump_all(docs, path)
    log(f"retagged {changed} container image(s) in {path}")
    return changed


def workload_images(path: Path, image: str) -> dict[tuple[str, str], str]:
    """{(deployment name, container name): image ref} for this app's own containers.

    Datastore images (a provisioner's postgres/redis) are skipped: they are decided by the
    catalog, not by what the app built, so promoting must not touch them.
    """
    pattern = re.compile(rf"/{re.escape(image)}[:-]")
    found = {}
    for doc in load_all(path):
        if doc.get("kind") != "Deployment":
            continue
        name = doc["metadata"]["name"]
        for container in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []:
            ref = container.get("image", "")
            if pattern.search(ref):
                found[(name, container.get("name", ""))] = ref
    return found


def copy_images(src: Path, dst: Path, image: str) -> int:
    """Make dst run exactly the images src is running. Returns how many changed.

    This is what promoting a MULTI-WORKLOAD app means once each service carries its own
    content-derived tag: there is no single "version" to move prod to, there is a SET of
    eleven image references, and prod should run precisely the set staging was verified on.
    """
    wanted = workload_images(src, image)
    docs = load_all(dst)
    changed = 0
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        name = doc["metadata"]["name"]
        for container in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []:
            ref = wanted.get((name, container.get("name", "")))
            if ref and container.get("image") != ref:
                container["image"] = ref
                changed += 1
    dump_all(docs, dst)
    log(f"copied {changed} image(s) from {src} into {dst}")
    return changed


def cmd_promote(args) -> None:
    config = Path(args.config_dir)
    target = config / "prod" / "manifests.yaml"

    if args.mode in ("from-staging", "tag-only"):
        # Both modes skip the renderer, so both can silently ship stale prod values.
        guard_prod_values(args)

    if args.mode == "from-staging":
        # Prod runs exactly what staging runs. The only mode that is correct when each
        # service has its own tag, because then "promote to version X" is not a single value.
        source = config / "staging" / "manifests.yaml"
        if not source.is_file():
            raise SystemExit(f"{source} missing — nothing has been deployed to staging yet")
        if not target.is_file():
            raise SystemExit(f"{target} missing — run --mode re-render first")
        if not copy_images(source, target, args.image):
            log("prod already runs the same images as staging")
        return

    if args.mode == "tag-only":
        # Every workload moved to ONE tag. Correct for a single-workload app; for a repo of
        # many services use from-staging instead, or this will point them all at a tag only
        # one of them actually has.
        if not target.is_file():
            raise SystemExit(f"{target} missing — run --mode re-render first")
        if not retag(target, args.image, args.tag):
            warn(f"no image matching /{args.image} found in {target}")
        return

    for flag in ("catalog", "app_dir", "work"):
        if not getattr(args, flag, None):
            raise SystemExit(f"--{flag.replace('_', '-')} is required for --mode re-render")
    render_args = argparse.Namespace(**vars(args))
    render_args.env = "prod"
    render_args.out = str(target)
    cmd_render(render_args)



def cmd_verify(args) -> None:
    """Chờ tới khi cụm THỰC SỰ chạy đúng thứ vừa render. Hết giờ là fail kèm chẩn đoán.

    Lý do có bước này, đo từ một sự cố thật: đổi cách đặt tên ảnh làm 5 app cùng
    ImagePullBackOff, mà TOÀN BỘ pipeline vẫn báo thành công — CI xanh, orchestrator xanh,
    manifest ghi đúng vào config repo, Fleet apply không lỗi. Chỉ pod là chết. Không ai
    phát hiện cho tới khi có người mở trang web.

    Bước đối chiếu trước đó chỉ so SHA giữa config repo và nhánh app — nó không nhìn vào
    cụm, nên không thể bắt được loại lỗi này. Đây là chỗ duy nhất trong toàn luồng thực sự
    hỏi "ứng dụng có chạy không".
    """
    ns = app_namespace(args.app, args.env)
    docs = load_all(Path(args.manifests))

    # Secrets first, and on purpose. A workload whose Secret has not synced sits in
    # CreateContainerConfigError, which the rollout check below reports as "0/1 replicas
    # ready" — true, useless, and it sends whoever is paged to look at the image. Checking
    # the VaultStaticSecret first turns the same failure into "Vault path X, reason Y".
    wait_for_vault_secrets(docs, ns, args)
    # Then the database, for the same reason: an app whose database has not finished
    # bootstrapping crash-loops on connection refused, and the rollout check would report
    # that as "0 replicas ready" without ever mentioning the database.
    wait_for_databases(docs, ns, args)

    want: dict[str, list[str]] = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        want[doc["metadata"]["name"]] = [c.get("image") for c in containers]

    if not want:
        log("không có Deployment nào để kiểm — bỏ qua")
        return


    log(f"chờ {len(want)} Deployment trong {ns} chạy đúng ảnh vừa render "
        f"(tối đa {args.timeout}s)")
    deadline = time.time() + args.timeout
    while True:
        pending = []
        for name, images in sorted(want.items()):
            cp = kubectl(["get", "deploy", name, "-n", ns, "-o", "json"],
                         kubeconfig=args.kubeconfig, check=False, capture=True)
            if cp.returncode != 0:
                pending.append(f"{name}: chưa tồn tại trên cụm")
                continue
            obj = json.loads(cp.stdout)
            live = [c.get("image") for c in
                    obj["spec"]["template"]["spec"].get("containers", [])]
            if live != images:
                pending.append(f"{name}: đang chạy {live}, cần {images}")
                continue
            # Đủ pod sẵn sàng KHÔNG có nghĩa là bản mới đã lên. Đo được từ một lần thử
            # cố ý: đặt nhãn ảnh không tồn tại thì Kubernetes tạo pod mới, pod đó
            # ImagePullBackOff, còn 3 pod CŨ vẫn chạy nguyên. availableReplicas vẫn là
            # 3/3 nên phép kiểm cũ báo xanh — đúng cái sự cố mà bước này sinh ra để bắt.
            # Phải hỏi "bản mới đã thay xong bản cũ chưa", tức đúng câu hỏi mà
            # `kubectl rollout status` hỏi:
            st = obj.get("status") or {}
            need = (obj.get("spec") or {}).get("replicas", 1) or 1
            gen = (obj.get("metadata") or {}).get("generation", 0) or 0
            observed = st.get("observedGeneration", 0) or 0
            updated = st.get("updatedReplicas", 0) or 0
            avail = st.get("availableReplicas", 0) or 0
            total = st.get("replicas", 0) or 0
            if observed < gen:
                pending.append(f"{name}: Kubernetes chưa xử lý bản sửa mới nhất")
            elif updated < need:
                pending.append(
                    f"{name}: mới {updated}/{need} bản sao chạy phiên bản MỚI "
                    f"(còn {total - updated} bản sao cũ đang phục vụ)")
            elif total > updated:
                pending.append(
                    f"{name}: bản cũ chưa được thu hồi ({total - updated} bản sao thừa)")
            elif avail < need:
                pending.append(f"{name}: mới {avail}/{need} bản sao sẵn sàng")
        if not pending:
            log(f"tất cả {len(want)} Deployment trong {ns} đã chạy đúng ảnh vừa render")
            return
        if time.time() >= deadline:
            break
        time.sleep(poll_interval(10))

    # Hết giờ: in đúng thứ cần để hiểu vì sao, ngay tại chỗ.
    warn(f"sau {args.timeout}s vẫn chưa đạt trạng thái mong muốn trong {ns}")
    for line in pending:
        print(f"::error::{line}", file=sys.stderr, flush=True)
    kubectl(["get", "pods", "-n", ns], kubeconfig=args.kubeconfig, check=False)
    kubectl(["get", "events", "-n", ns, "--sort-by=.lastTimestamp"],
            kubeconfig=args.kubeconfig, check=False)
    raise SystemExit(
        f"{args.app}/{args.env}: manifest đã ghi và Fleet đã đồng bộ, nhưng cụm KHÔNG "
        "chạy đúng thứ vừa render. Xem danh sách pod và event ở trên."
    )
