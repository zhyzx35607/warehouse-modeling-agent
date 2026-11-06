/**
 * Plain-language hints for technical task error messages.
 * Mirrors wren_agent/nodes/reporting.py _ERROR_RULES — keep the two in sync.
 */

const ERROR_RULES = [
  ['schema exploration produced no tables',
   '在源数据库中没有找到可用的数据表。常见原因：表名过滤条件过严、连接的不是预期的库，或该库里确实没有表。'],
  ['critical design invariants failed',
   '自动检查设计稿时发现了必须修正的问题，系统已停止继续执行。详见报告中的问题清单。'],
  ['auto mode: execution errors persisted',
   '自动模式下系统已尝试自动修复多轮，但执行错误仍未消除，需要人工介入处理。'],
  ['missing design plan',
   '设计阶段没有产出可检查的方案。'],
  ['no sql artifacts',
   '设计阶段没有生成任何建表或数据装载 SQL。'],
  ['breaking schema drift',
   '源表结构发生了不兼容变更，且该变更未获批准。为安全起见，任务已停止。'],
  ['graph_recursion_limit',
   '任务内部重试次数达到系统上限，已被安全停止。'],
  ['insufficient_warehouse',
   '现有数仓里没有足够的基础表来支撑这次建模目标。'],
]

/** Translate a technical error message into plain language. */
export function translateError(message) {
  const text = String(message || '')
  const lower = text.toLowerCase()
  for (const [needle, plain] of ERROR_RULES) {
    if (lower.includes(needle)) return plain
  }
  return text ? `${text}（技术细节）` : ''
}
