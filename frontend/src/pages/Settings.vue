<template>
  <div>
    <div class="hero"><div><h1>设置</h1><p>拨到货日（夹具日，非墙钟强绑）与全局对账口径。</p></div></div>

    <div class="panel">
      <h2>到货日</h2>
      <div class="row">
        <input v-model="day" placeholder="YYYY-MM-DD" />
        <button @click="saveDay">保存到货日</button>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
    </div>

    <div class="panel">
      <h2>对账口径（全局只贯彻一种）</h2>
      <p class="muted">
        切换只影响<strong>之后新建</strong>的对账批次；已有批次（含已封批）永久保留创建时的口径与别名版本，
        因此切换前后两种架构的结果可在三联对账页直接对照。
      </p>
      <div class="row">
        <label class="mode-card" :class="{ chosen: mode === 'alias_merge' }">
          <input type="radio" value="alias_merge" v-model="mode" />
          <span>
            <strong>别名归并</strong><br />
            外部码经 alias 表映射到内部 sku 再比三腿；映射失败进 alias_miss（不得进 matched）；
            批次快照钉住 alias 版本与全量映射。
          </span>
        </label>
        <label class="mode-card" :class="{ chosen: mode === 'strict' }">
          <input type="radio" value="strict" v-model="mode" />
          <span>
            <strong>严格码</strong><br />
            只按原始码比三腿，别名表仅展示；别名造成的表面差额进普通差额人工处置，禁止静默映射改键。
          </span>
        </label>
      </div>
      <div class="row">
        <button @click="saveMode">保存口径</button>
        <span class="tag seal">当前：{{ mode === 'alias_merge' ? '别名归并' : '严格码' }}</span>
        <span class="tag ok">alias 版本 v{{ aliasVersion }}</span>
      </div>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const day = ref('')
const mode = ref('alias_merge')
const aliasVersion = ref(0)
const err = ref('')
const ok = ref('')
async function load() {
  const s = await api('/settings')
  day.value = s.dock_day || ''
  mode.value = s.reconcile_mode || 'alias_merge'
  aliasVersion.value = s.alias_version || 0
}
async function saveDay() {
  err.value = ''; ok.value = ''
  try {
    await api('/settings/dock-day', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '到货日已保存'
  } catch (e) { err.value = e.message }
}
async function saveMode() {
  err.value = ''; ok.value = ''
  try {
    const r = await api('/settings/reconcile-mode', {
      method: 'POST', body: JSON.stringify({ mode: mode.value }),
    })
    mode.value = r.reconcile_mode
    ok.value = '口径已保存，仅影响之后新建的批次'
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<style scoped>
.mode-card {
  display: flex; gap: 10px; align-items: flex-start;
  border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px; min-width: 320px; max-width: 460px;
  background: #fffdf8; cursor: pointer;
}
.mode-card.chosen { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
.mode-card input { min-width: auto; margin-top: 4px; }
.muted { color: var(--muted); font-size: 0.9rem; }
</style>
