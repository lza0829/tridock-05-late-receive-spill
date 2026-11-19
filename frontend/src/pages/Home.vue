<template>
  <div>
    <div class="hero">
      <div>
        <h1>总览</h1>
        <p>当前到货日三腿行数、对账批次与结案状态。所有计数与对账物化同源，取批次快照数字，不另起重算。
          结案后当日预报/实收/回执不可再改。</p>
      </div>
      <button @click="load">刷新</button>
    </div>
    <div class="grid" v-if="d">
      <div class="stat"><div class="k">到货日</div><div class="v">{{ d.dock_day }}</div></div>
      <div class="stat">
        <div class="k">对账口径</div>
        <div class="v" style="font-size:1.15rem">
          <span class="tag" :class="d.reconcile_mode === 'alias_merge' ? 'ok' : 'seal'">
            {{ d.reconcile_mode === 'alias_merge' ? '别名归并' : '严格码' }}
          </span>
        </div>
      </div>
      <div class="stat"><div class="k">预报 / 实收 / 回执</div><div class="v">{{ d.expect_count }} / {{ d.receive_count }} / {{ d.confirm_count }}</div></div>
      <div class="stat">
        <div class="k">未决差额 / 缺映射</div>
        <div class="v">
          {{ d.pending_variance }}
          <span :class="d.alias_miss_count ? '' : 'okmsg'"> / {{ d.alias_miss_count }}</span>
        </div>
      </div>
      <div class="stat">
        <div class="k">结案</div>
        <div class="v"><span class="tag" :class="d.sealed ? 'seal' : 'bad'">{{ d.sealed ? '已结案' : '未结案' }}</span></div>
      </div>
    </div>
    <div class="panel" v-if="d && d.latest_batch">
      <h2>最近对账批次</h2>
      <p>
        {{ d.latest_batch.batch_no }} ·
        {{ d.latest_batch.mode === 'alias_merge' ? '别名归并' : '严格码' }}
        <template v-if="d.latest_batch.alias_version"> · 钉版 v{{ d.latest_batch.alias_version }}</template>
        · 对齐 {{ d.latest_batch.matched_count }} · 归并键 {{ d.latest_batch.mapped_count }}
        · 缺映射 {{ d.latest_batch.alias_miss_count }} · 差额 {{ d.latest_batch.variance_count }}
        · {{ d.latest_batch.status }}
      </p>
    </div>
    <p class="msg">{{ err }}</p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const d = ref(null)
const err = ref('')
async function load() {
  err.value = ''
  try { d.value = await api('/dashboard') } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
