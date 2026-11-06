<template>
  <section class="demo-page">
    <p class="eyebrow">WAREHOUSE MODELING / OFFLINE LAB</p>
    <h1>让数据走完一座数仓</h1>
    <p>从合成电商订单出发，读取 SQLite 的真实 schema，执行 ODS → DIM → DWD → DWS → ADS，检查金额与粒度，再查看血缘。这里使用确定性规则，不调用大模型。</p>
    <div class="controls">
      <label>订单数 <input v-model.number="orders" type="number" min="1" max="10000"></label>
      <label>随机种子 <input v-model.number="seed" type="number"></label>
      <button :disabled="busy" @click="execute">{{ busy ? '正在建模与校验…' : '运行离线演示' }}</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <template v-if="report">
      <div class="stats"><article><strong>{{ report.models.length }}</strong><span>已落表模型</span></article><article><strong>{{ report.checks.filter(c=>c.passed).length }}/{{ report.checks.length }}</strong><span>质量检查通过</span></article><article><strong>{{ report.source_orders }}</strong><span>合成订单</span></article></div>
      <p>结果：{{ report.status }} · 产物保存在 {{ report.output }}</p>
      <h2>模型与行数</h2>
      <div class="models"><article v-for="m in report.models" :key="m.name"><small>{{ m.layer }}</small><h3>{{ m.name }}</h3><p>{{ report.row_counts[m.name] }} 行 · 粒度 {{ m.grain.join(' × ') }}</p><details><summary>查看实际执行的 SELECT</summary><pre>{{ m.select }}</pre></details></article></div>
      <h2>质量检查</h2><div class="table-wrap"><table><thead><tr><th>检查</th><th>结果</th><th>实际值</th><th>期望值</th></tr></thead><tbody><tr v-for="check in report.checks" :key="check.name"><td>{{ check.name }}</td><td :class="check.passed?'passed':'error'">{{ check.passed?'通过':'失败' }}</td><td>{{ check.actual }}</td><td>{{ check.expected }}</td></tr></tbody></table></div>
      <h2>每日销售</h2><div class="table-wrap"><table><thead><tr><th>日期</th><th>订单数</th><th>应收金额</th><th>实收金额</th></tr></thead><tbody><tr v-for="row in report.daily_sales" :key="row.order_date"><td>{{ row.order_date }}</td><td>{{ row.order_count }}</td><td>{{ (row.gross_cents/100).toFixed(2) }}</td><td>{{ (row.paid_cents/100).toFixed(2) }}</td></tr></tbody></table></div>
      <h2>表级血缘</h2><ul><li v-for="edge in report.lineage" :key="edge.source+edge.target"><code>{{ edge.source }}</code> → <code>{{ edge.target }}</code></li></ul>
    </template>
  </section>
</template>
<script setup>
import { ref } from 'vue'
const orders=ref(120), seed=ref(7), busy=ref(false), error=ref(''), report=ref(null)
async function execute(){
  busy.value=true; error.value=''; report.value=null
  try {
    const response=await fetch('/api/demo/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({orders:orders.value,seed:seed.value})})
    const result=await response.json()
    if(!response.ok)throw new Error(result.detail || '执行失败，请查看本地终端。')
    report.value=result
  }catch(exc){error.value=exc.message}finally{busy.value=false}
}
</script>
<style scoped>
.demo-page{max-width:1120px;margin:40px auto;padding:0 24px 80px}.eyebrow{font-size:12px;letter-spacing:.2em;color:#087f8c}h1{font-size:clamp(30px,4vw,48px);letter-spacing:-.03em;margin:16px 0}h2{margin:36px 0 16px}p{line-height:1.8;color:var(--text-secondary,#546374)}.controls{display:flex;gap:20px;flex-wrap:wrap;align-items:end;margin:28px 0}label{display:grid;gap:6px}input{width:130px;padding:10px;border:1px solid #bfcbd2;border-radius:8px;color:inherit;background:transparent}button{padding:12px 22px;border:0;border-radius:8px;background:#087f8c;color:white;cursor:pointer}button:disabled{opacity:.6}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.stats article,.models article{border:1px solid #cbd5dc;border-radius:12px;padding:20px}.stats strong{display:block;font-size:32px}.stats span,small{color:#087f8c}.models{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}h3{font-size:17px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.6}summary{cursor:pointer;color:#087f8c}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}td,th{border-bottom:1px solid #cbd5dc;padding:10px;text-align:left}.passed{color:#087f54}.error{color:#b42318}li{padding:5px 0}code{font-size:13px}@media(max-width:600px){.stats{grid-template-columns:1fr}.controls{gap:12px}}
</style>
