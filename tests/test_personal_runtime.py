from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest
from wren_agent.config import PostgresSettings, LLMSettings, settings
from wren_agent.models.design import DWDesignPlan
from wren_agent.state import create_initial_state


def test_tls_policy_is_not_overridden():
    for dsn in ['postgresql://localhost/warehouse', 'postgresql://localhost/warehouse?sslmode=require']:
        assert PostgresSettings(dsn=dsn).safe_dsn == dsn


def test_configuration_preserves_python_properties():
    assert isinstance(LLMSettings.is_configured, property)
    assert LLMSettings(api_key='').is_configured is False
    assert LLMSettings(api_key='placeholder').is_configured is True


@pytest.mark.asyncio
@pytest.mark.parametrize('layer', ['ods','dim','dwd'])
async def test_live_model_failure_never_becomes_demo_success(monkeypatch, layer):
    import wren_agent.nodes.design_layers as nodes
    monkeypatch.setattr(settings.llm,'allow_programmatic_fallback',False)
    monkeypatch.setattr(nodes,'_llm_generate_layer',AsyncMock(side_effect=RuntimeError('provider unavailable')))
    fallback=AsyncMock()
    monkeypatch.setattr(nodes,f'_design_{layer}_programmatic',fallback)
    state=create_initial_state('strict-test','Build a sales warehouse',[],{})
    state['design_plan']=DWDesignPlan(task_id='strict-test',modeling_goal='sales')
    with pytest.raises(RuntimeError,match='provider unavailable'):
        await getattr(nodes,f'design_{layer}')(state)
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_embedding_disabled_without_its_own_endpoint(monkeypatch):
    from wren_agent.tools.embedding import embed_texts
    monkeypatch.setattr(settings.llm,'embedding_api_base','')
    assert await embed_texts(['orders.order_id']) == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize('circuit_open', [False, True])
async def test_live_recovery_cannot_skip_a_failed_design(monkeypatch, circuit_open):
    import wren_agent.tools.error_recovery as recovery
    monkeypatch.setattr(settings.llm, 'allow_programmatic_fallback', False)
    monkeypatch.setattr(recovery, 'get_recovery_config', lambda _: recovery.NodeRecoveryConfig(
        max_attempts=1, service_name='llm', on_final_failure='skip'
    ))
    breaker = SimpleNamespace(
        is_open=circuit_open, record_success=lambda: None, record_failure=lambda _: None
    )
    monkeypatch.setattr(recovery, 'get_breaker', lambda *args, **kwargs: breaker)
    node = AsyncMock(side_effect=RuntimeError('provider unavailable'))
    wrapped = recovery.wrap_node_with_recovery(node, 'design_ods')
    with pytest.raises(RuntimeError, match='circuit is open' if circuit_open else 'provider unavailable'):
        await wrapped(create_initial_state('strict-wrapper', 'sales', [], {}))
    assert node.await_count == (0 if circuit_open else 1)
