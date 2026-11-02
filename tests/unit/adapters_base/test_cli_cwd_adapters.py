"""兩個 CLI adapter 的工作目錄失敗 → 該次 error(決策 20)。"""
import pytest

from spillway.adapters.base import AdapterContext, ChatRequest, Deployment
from spillway.adapters.claude_cli import create as create_claude
from spillway.adapters.codex_cli import create as create_codex
from spillway.clock import FakeClock

pytestmark = pytest.mark.anyio


class _Boom:
    async def run(self, *a, **k):
        raise AssertionError("不應起子行程")


def _ctx(data_dir):
    return AdapterContext(credentials=lambda ref: "unused", clock=FakeClock(), cli_runner=_Boom(),
                          data_dir=data_dir)


def _dep(name, model):
    return Deployment(id=name, model_name=name, kind="cli", provider=f"{name}-cli", model=model, params={})


CASES = [(create_claude, "claude", "claude-sonnet-5-5"), (create_codex, "codex", "gpt-5.5")]


@pytest.mark.parametrize("create,name,model", CASES)
async def test_no_data_dir_is_error(create, name, model):
    req = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    result = await create(_ctx(None)).call(_dep(name, model), req, None)
    assert result.status == "error" and not result.ok


@pytest.mark.parametrize("create,name,model", CASES)
async def test_mkdir_failure_is_error(create, name, model, tmp_path):
    (tmp_path / "work").write_text("file")
    req = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    result = await create(_ctx(tmp_path)).call(_dep(name, model), req, None)
    assert result.status == "error" and not result.ok
