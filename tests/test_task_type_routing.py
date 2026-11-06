from wren_agent.graph import route_after_dwd_design, route_after_ods_design


def test_ods_task_stops_after_ods_design():
    assert route_after_ods_design({"business_task_type": "ods_migration"}) == "harness_validate"


def test_core_model_task_skips_ods_but_stops_after_dwd():
    assert route_after_ods_design({"business_task_type": "core_model"}) == "design_dim"
    assert route_after_dwd_design({"business_task_type": "core_model"}) == "harness_validate"


def test_legacy_business_task_keeps_full_chain():
    assert route_after_ods_design({"business_task_type": "full"}) == "design_dim"
    assert route_after_dwd_design({"business_task_type": "full"}) == "design_dws"


def test_whole_db_goal_on_full_type_is_ods_migration():
    """Dashboard template used to land as business_task_type=full (9fcfcdbb)."""
    goal = "将所选业务源库中的全部业务表一对一迁移到 Doris ODS。"
    state = {"business_task_type": "full", "modeling_goal": goal}
    assert route_after_ods_design(state) == "harness_validate"
    assert route_after_dwd_design(state) == "harness_validate"
