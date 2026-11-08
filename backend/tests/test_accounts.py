"""Phase 2a storage side: encryption at rest, the accounts migration's data
move, encrypted per-repo tokens, and where a scan's credentials come from."""
import datetime as dt

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from app import accounts, crypto, db, pipeline, scan_context, storage
from app.agents import clone, source_apis
from app.agents.clone import Clone
from app.config import get_settings
from app.db import session
from app.db_models import RepoRow, WorkspaceRow
from app.models import LOCAL_WORKSPACE_ID, PackageList, Repo


def _env(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    crypto.reset()


# --------------------------------------------------------------------------- #
# crypto                                                                        #
# --------------------------------------------------------------------------- #
def test_values_round_trip_and_are_prefixed():
    stored = crypto.encrypt("glpat-secret-token")
    assert stored.startswith("enc:v1:") and "glpat" not in stored
    assert crypto.decrypt(stored) == "glpat-secret-token"
    assert crypto.decrypt("legacy-plain-text") == "legacy-plain-text"  # pre-encryption rows


def test_keys_can_be_rotated(monkeypatch):
    _env(monkeypatch, APP_SECRET_KEY="old-key")
    stored = crypto.encrypt("token")
    _env(monkeypatch, APP_SECRET_KEY="new-key", APP_SECRET_KEY_PREVIOUS="old-key")
    assert crypto.decrypt(stored) == "token"
    _env(monkeypatch, APP_SECRET_KEY_PREVIOUS=None)
    with pytest.raises(crypto.SecretsUnavailable):
        crypto.decrypt(stored)


def test_the_public_mode_requires_a_key(monkeypatch):
    _env(monkeypatch, APP_SECRET_KEY=None, AUTH_MODE="github")
    with pytest.raises(crypto.SecretsUnavailable, match="APP_SECRET_KEY"):
        crypto.encrypt("x")


def test_a_local_install_generates_its_key_once(monkeypatch, tmp_path):
    key_file = tmp_path / "keys" / "secret.key"
    _env(monkeypatch, APP_SECRET_KEY=None, LOCAL_SECRET_KEY_FILE=str(key_file))
    stored = crypto.encrypt("token")
    first = key_file.read_text()
    crypto.reset()
    assert crypto.decrypt(stored) == "token" and key_file.read_text() == first


def test_hints_reveal_little():
    assert crypto.hint("short") == "…"
    assert crypto.hint("sk-ant-api03-abcdefgh1234") == "…1234"


# --------------------------------------------------------------------------- #
# migration 0008                                                                #
# --------------------------------------------------------------------------- #
def test_the_migration_moves_existing_data_into_the_local_workspace(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/mig.db"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    db.reset_engine()
    command.upgrade(db.alembic_config(), "0007")
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO repos (id, position, url, source, interval_days, image_tags, "
                       "detected_tag_vars, ignored_packages, last_scan_by_branch) VALUES "
                       "('old', 1, 'https://x/old', 'git', 1, '{}', '[]', '[]', '{}')"))
        c.execute(text("INSERT INTO app_settings (key, value) VALUES ('use_enrichment_agent', 'false')"))
    command.upgrade(db.alembic_config(), "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT workspace_id FROM repos WHERE id='old'")).scalar() == "local"
        assert c.execute(text("SELECT role FROM memberships WHERE workspace_id='local' "
                              "AND user_id='local'")).scalar() == "owner"
        assert c.execute(text("SELECT platform_role FROM users WHERE id='local'")).scalar() == "admin"
        assert c.execute(text("SELECT value FROM workspace_settings WHERE workspace_id='local' "
                              "AND key='use_enrichment_agent'")).scalar() == "false"
    db.reset_engine()


# --------------------------------------------------------------------------- #
# per-repo tokens                                                               #
# --------------------------------------------------------------------------- #
def test_repo_tokens_are_encrypted_at_rest():
    storage.upsert_repo(Repo(id="tok", url="https://gitlab.com/x/y", branches=["main"],
                             token_override="glpat-abcdefghijkl"))
    with session() as s:
        raw = s.get(RepoRow, "tok").token_override
    assert raw.startswith("enc:v1:") and "glpat" not in raw
    assert storage.get_repo("tok").token_override == "glpat-abcdefghijkl"


def test_legacy_plain_text_tokens_are_encrypted_at_startup():
    storage.upsert_repo(Repo(id="legacy", url="https://gitlab.com/x/y", branches=["main"]))
    with session() as s:
        s.get(RepoRow, "legacy").token_override = "plain-token"
    assert storage.get_repo("legacy").token_override == "plain-token"  # still readable
    assert storage.encrypt_plaintext_tokens() == 1
    with session() as s:
        assert s.get(RepoRow, "legacy").token_override.startswith("enc:v1:")
    assert storage.get_repo("legacy").token_override == "plain-token"
    assert storage.encrypt_plaintext_tokens() == 0


def test_repos_are_listed_per_workspace():
    storage.upsert_repo(Repo(id="mine", url="https://x/a", branches=["main"], workspace_id="ws-a"))
    storage.upsert_repo(Repo(id="local-one", url="https://x/b", branches=["main"]))
    assert [r.id for r in storage.load_repos("ws-a")] == ["mine"]
    assert [r.id for r in storage.load_repos(LOCAL_WORKSPACE_ID)] == ["local-one"]
    assert {r.id for r in storage.load_repos()} == {"mine", "local-one"}


# --------------------------------------------------------------------------- #
# where a scan's credentials come from                                          #
# --------------------------------------------------------------------------- #
def _workspace(ws_id: str) -> None:
    with session() as s:
        s.add(WorkspaceRow(id=ws_id, name=ws_id, personal=False,
                           created_at=dt.datetime.now(dt.timezone.utc)))


def test_github_api_token_prefers_workspace_then_creator_then_local_env(monkeypatch):
    _env(monkeypatch, GITHUB_API_TOKEN="env-token")
    _workspace("ws1")
    user, _ = accounts.sign_in_github_user({"id": 7, "login": "maker"}, "gho_creator", admin=False)
    ctx = scan_context.ScanContext(workspace_id="ws1", created_by=user.id)
    assert ctx.github_api_token() == "gho_creator"
    accounts.set_secret("ws1", "github_token", "ghp_workspace_token")
    assert scan_context.ScanContext(workspace_id="ws1", created_by=user.id).github_api_token() == "ghp_workspace_token"
    assert scan_context.ScanContext().github_api_token() == "env-token"  # local mode, local workspace


def test_the_public_mode_never_falls_back_to_the_servers_tokens(monkeypatch):
    _env(monkeypatch, AUTH_MODE="github", GITHUB_API_TOKEN="env-token", GITLAB_TOKEN="env-gl")
    _workspace("ws2")
    ctx = scan_context.ScanContext(workspace_id="ws2")
    assert ctx.github_api_token() is None
    assert ctx.git_token("gitlab.com") is None


async def test_clone_and_github_lookups_use_the_bound_workspace(monkeypatch, tmp_path):
    _env(monkeypatch, AUTH_MODE="github")
    _workspace("ws3")
    accounts.set_secret("ws3", "gitlab_token", "glpat-workspace-1234")
    accounts.set_secret("ws3", "github_token", "ghp-workspace-api")
    seen = {}
    (tmp_path / "repo").mkdir()

    def fake_clone(url, branch, token=None):
        seen["clone_token"] = clone._token_for(url, token)
        seen["api_token"] = scan_context.github_api_token()
        return Clone(path=tmp_path / "repo", _tmp=str(tmp_path / "unused"))

    async def fake_extract(clone_path, *, use_agent=False, tag_vars=None):
        seen["workspace"] = scan_context.workspace_id()
        return PackageList(packages=[])

    monkeypatch.setattr(pipeline, "shallow_clone", fake_clone)
    monkeypatch.setattr(Clone, "cleanup", lambda self: None)
    monkeypatch.setattr(pipeline.extraction_agent, "extract", fake_extract)
    repo = Repo(id="scoped", url="https://gitlab.com/team/app.git", branches=["main"], workspace_id="ws3")
    await pipeline.run_scan(repo, "main", use_extraction_agent=False)

    assert seen == {"clone_token": "glpat-workspace-1234", "api_token": "ghp-workspace-api",
                    "workspace": "ws3"}
    assert storage.list_scan_runs("scoped")[0].workspace_id == "ws3"
    assert source_apis is not None
