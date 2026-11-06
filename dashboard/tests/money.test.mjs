import test from 'node:test'
import assert from 'node:assert/strict'
import { formatCents } from '../src/utils/money.js'

test('large transmitted cents keep the final one cent', () => {
  const payload = JSON.parse('{"paid_cents":"10000000000000001"}')
  assert.equal(formatCents(payload.paid_cents), '100000000000000.01')
})
test('signed amounts and ordinary cents format without floating point division', () => {
  assert.equal(formatCents(-1), '-0.01')
  assert.equal(formatCents(10000), '100.00')
  assert.equal(formatCents('0'), '0.00')
})
test('already unsafe JavaScript numbers are not presented as precise evidence', () => {
  assert.equal(formatCents(10000000000000001), '数值精度不可验证')
  assert.equal(formatCents('bad'), '无效金额')
})
