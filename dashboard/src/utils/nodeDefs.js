/**
 * Single source of truth for the agent graph node list.
 *
 * Keys/phases only — labels are resolved via i18n in components
 * (`components.nodeTimeline.nodes.<key>`) so they stay reactive to
 * locale switches.
 */
export const NODE_DEFS = [
  { key: 'receive_task', phase: 'init' },
  { key: 'blueprint_match', phase: 'blueprint_match' },
  { key: 'schema_explore_light', phase: 'schema_explore_light' },
  { key: 'detect_sensitive_fields', phase: 'schema_explore_light' },
  { key: 'confirm_sensitive', phase: 'schema_explore_light' },
  { key: 'deep_explore', phase: 'schema_explore_deep' },
  { key: 'human_confirm_deep', phase: 'schema_explore_deep' },
  { key: 'grill_clarifications', phase: 'grill' },
  { key: 'wait_human', phase: 'grill' },
  { key: 'detect_schema_drifts', phase: 'design' },
  { key: 'generate_drift_fixes', phase: 'design' },
  { key: 'design_ods', phase: 'design' },
  { key: 'design_dim', phase: 'design' },
  { key: 'design_dwd', phase: 'design' },
  { key: 'design_dws', phase: 'design' },
  { key: 'design_ads', phase: 'design' },
  { key: 'harness_validate', phase: 'validate' },
  { key: 'design_revise', phase: 'validate' },
  { key: 'human_approval', phase: 'approval' },
  { key: 'process_approval', phase: 'approval' },
  { key: 'dag_deploy', phase: 'deploy' },
  { key: 'execute_tasks', phase: 'execute' },
  { key: 'classify_errors', phase: 'execute' },
  { key: 'retry_execution', phase: 'execute' },
  { key: 'final_validate', phase: 'final' },
  { key: 'error_handling', phase: 'final' },
  { key: 'quality_monitor', phase: 'report' },
  { key: 'generate_report', phase: 'report' },
  { key: 'export_kg', phase: 'report' },
]

export const TOTAL_NODES = NODE_DEFS.length
