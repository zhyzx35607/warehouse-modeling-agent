import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

function setup() {
  const source = readFileSync(new URL('../src/views/Demo.vue', import.meta.url), 'utf8')
    .split('<script setup>')[1].split('</script>')[0].replace(/^import .*$/gm, '')
  const requests = []
  const context = vm.createContext({
    ref: value => ({ value }), computed: getter => ({ get value() { return getter() } }),
    onMounted() {}, TextDecoder, encodeURIComponent,
    fetch: async (url, options) => {
      requests.push({ url, options })
      return { ok: true, json: async () => url.endsWith('/runs') ? [] : { run_id: 'new', status: 'passed' } }
    },
  })
  vm.runInContext(source + '\nglobalThis.subject={files,filesLoading,filesReady,mode,loadFiles,execute}', context)
  return { ...context.subject, requests }
}
const names = ['customers', 'products', 'orders', 'order_items', 'payments']
const uploads = (text, first) => names.map((name, index) => ({
  name: name + '.csv', size: 3,
  arrayBuffer: () => index === 0 && first ? first : Promise.resolve(new TextEncoder().encode(text).buffer),
}))

test('new upload blocks submission of previous CSV until every file is read', async () => {
  const ui = setup()
  ui.mode.value = 'csv'
  ui.files.value = Object.fromEntries(names.map(name => [name, 'OLD']))
  let resolve
  const pending = ui.loadFiles(uploads('NEW', new Promise(done => { resolve = done })))
  assert.equal(ui.filesReady.value, false)
  await ui.execute()
  assert.equal(ui.requests.length, 0)
  resolve(new TextEncoder().encode('NEW').buffer)
  await pending
  assert.equal(ui.filesReady.value, true)
  await ui.execute()
  const body = JSON.parse(ui.requests[0].options.body)
  assert.ok(names.every(name => body.tables[name] === 'NEW'))
})

test('an earlier upload completing late cannot replace the most recent selection', async () => {
  const ui = setup()
  let resolve
  const old = ui.loadFiles(uploads('OLD', new Promise(done => { resolve = done })))
  await ui.loadFiles(uploads('NEW'))
  resolve(new TextEncoder().encode('OLD').buffer)
  await old
  assert.equal(ui.filesLoading.value, false)
  assert.ok(names.every(name => ui.files.value[name] === 'NEW'))
})
