// Integer cents can arrive as a JSON integer or a decimal string for large values.
export function formatCents(value) {
  try {
    if (typeof value === 'number' && !Number.isSafeInteger(value)) return '数值精度不可验证'
    const cents = BigInt(value)
    const amount = cents < 0n ? -cents : cents
    return `${cents < 0n ? '-' : ''}${amount / 100n}.${String(amount % 100n).padStart(2, '0')}`
  } catch {
    return '无效金额'
  }
}
