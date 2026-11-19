<template>
  <div>
    <div class="hero">
      <div>
        <h1>别名维护</h1>
        <p>供应商外部码 → 内部货号，仅在同一供应商作用域内生效。每次维护推进 alias 版本；
          对账批次钉住运行时版本，事后改别名撬不动已封批次。</p>
      </div>
      <button @click="load">刷新</button>
    </div>

    <div class="panel">
      <div class="row">
        <span class="tag seal">当前口径：{{ mode === 'alias_merge' ? '别名归并' : '严格码（别名仅展示）' }}</span>
        <span class="tag ok">alias 版本 v{{ version }}</span>
      </div>
    </div>

    <div class="panel">
      <h2>新建 / 修改别名</h2>
      <div class="row">
        <input v-model="form.supplier_code" placeholder="供应商编码（如 SUP-A）" />
        <input v-model="form.external_sku" placeholder="外部码（供应商回执码）" />
        <input v-model="form.internal_sku" placeholder="内部货号" />
        <button @click="save">保存别名</button>
        <button class="secondary" @click="preview">预览解析</button>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
      <p v-if="previewResult" class="okmsg">
        预览：{{ form.external_sku }} → {{ previewResult.sku }}
        （{{ statusText(previewResult.status) }}，基于 v{{ previewResult.alias_version ?? '-' }}）
      </p>
    </div>

    <div class="panel">
      <h2>别名表</h2>
      <table>
        <thead>
          <tr><th>ID</th><th>供应商</th><th>外部码</th><th>内部货号</th><th>状态</th><th>更新时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.supplier_code }}</td>
            <td>{{ r.external_sku }}</td>
            <td>{{ r.internal_sku }}</td>
            <td><span class="tag" :class="r.active ? 'ok' : 'bad'">{{ r.active ? '生效' : '停用' }}</span></td>
            <td>{{ r.updated_at }}</td>
            <td>
              <button v-if="r.active" class="secondary" @click="deactivate(r.id)">停用</button>
              <span v-else class="tag bad">已停用</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rows = ref([])
const version = ref(0)
const mode = ref('alias_merge')
const form = ref({ supplier_code: 'SUP-A', external_sku: '', internal_sku: '' })
const previewResult = ref(null)
const err = ref('')
const ok = ref('')

function statusText(s) {
  return { alias_hit: '别名命中', identity: '内部码直通', miss: '缺映射（将进 alias_miss）', strict_raw: '严格码不映射' }[s] || s
}
async function load() {
  err.value = ''
  try {
    rows.value = await api('/aliases')
    const v = await api('/aliases/version')
    version.value = v.alias_version
    mode.value = v.reconcile_mode
  } catch (e) { err.value = e.message }
}
async function save() {
  err.value = ''; ok.value = ''; previewResult.value = null
  try {
    const r = await api('/aliases', { method: 'POST', body: JSON.stringify(form.value) })
    ok.value = `已保存，alias 版本推进到 v${r.version}`
    await load()
  } catch (e) { err.value = e.message }
}
async function preview() {
  err.value = ''; ok.value = ''
  try {
    previewResult.value = await api('/aliases/preview', {
      method: 'POST',
      body: JSON.stringify({ supplier_code: form.value.supplier_code, external_sku: form.value.external_sku }),
    })
  } catch (e) { err.value = e.message }
}
async function deactivate(id) {
  err.value = ''; ok.value = ''
  try {
    const r = await api(`/aliases/${id}`, { method: 'DELETE' })
    ok.value = `已停用，alias 版本推进到 v${r.version}`
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
