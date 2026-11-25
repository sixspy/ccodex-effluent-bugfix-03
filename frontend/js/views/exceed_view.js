export const exceedViewTemplate = `
  <div class="grid-2">
    <div class="panel">
      <h3>超标旁路挂起</h3>
      <table>
        <thead><tr><th>键</th><th>采样</th><th>指标</th><th>测值</th><th>挂起阈</th><th>当前口阈</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="h in exceeds" :key="h.id">
            <td>{{ h.hold_key }}</td><td>{{ h.sample_code }}</td><td>{{ h.exceed_metric }}</td>
            <td>{{ h.measured }}</td>
            <td :class="h.status==='开放' && h.limit_value!==h.outfall_cod_limit ? 'fail' : ''">{{ h.limit_value }}</td>
            <td>{{ h.outfall_cod_limit }}</td>
            <td :class="h.status==='开放'?'fail':'pass'">{{ h.status }}</td>
            <td><button v-if="h.status==='开放'" class="secondary" style="width:auto;margin:0" @click="closeExceed(h.id)">闭环</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h3>阈值加严</h3>
      <label>排放口 ID</label><input type="number" v-model.number="tightenForm.outfall_id"/>
      <label>新 COD 阈（须严于现阈，否则记加严偏航拒写）</label><input type="number" v-model.number="tightenForm.cod_limit"/>
      <div class="actions"><button @click="doTighten">加严并重开未结挂起</button></div>
      <p class="muted">加严与未结挂起重开同事务，否则整笔回退；同口并发抢超标锁，败方仅记争用痕零改。</p>
    </div>
  </div>`;
