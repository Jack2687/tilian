<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  Activity, ArrowLeft, CheckCircle2, Database, KeyRound, Layers3,
  LoaderCircle, LogOut, RefreshCw, Server, ShieldCheck, XCircle, Save, Megaphone, ScrollText, Route, TriangleAlert,
  Search, Filter, ChevronLeft, ChevronRight, UserRound, Settings2, Check, Pencil, Copy,
} from "lucide-vue-next";
import { api, ApiError } from "../api/client";
import { notifyRuntimeSettingsChanged } from "../runtimeSettings";
import type { AdminAnnouncement, AdminCard, AdminConcurrency, AdminLog, AdminOverview, AdminProxyPools, AdminRouteStat, AdminTask, AdminUser, ClusterStatus, UserPolicy } from "../types";

const proxyModeNames: Record<string, string> = { paypal: "PayPal", gopay: "GoPay", gcash: "GCash", momo: "MoMo", upi: "UPI", blik: "BLIK", pix: "PIX", ideal: "iDEAL", card: "直卡" };
const emptyProxyPools = (): AdminProxyPools => ({ channels: Object.fromEntries(Object.keys(proxyModeNames).map((mode) => [mode, { enabled: false, primaryText: "", exitText: "" }])) });

const TOKEN_KEY = "tilian:admin-token";
const tokenInput = ref("");
const token = ref(sessionStorage.getItem(TOKEN_KEY) || "");
const overview = ref<AdminOverview | null>(null);
const cluster = ref<ClusterStatus | null>(null);
const cards = ref<AdminCard[]>([]);
const tasks = ref<AdminTask[]>([]);
const users = ref<AdminUser[]>([]);
const usersTotal = ref(0);
const userQuery = ref("");
const userStatus = ref("all");
const userPage = ref(1);
const userPageSize = ref(20);
const usersLoading = ref(false);
const selectedUser = ref<AdminUser | null>(null);
const userEditorOpen = ref(false);
const userSaving = ref(false);
const userDraft = ref({ displayName: "", status: "active", maxAccountsPerTask: 100, allowFusion: true, notes: "" });
const userPolicy = ref<UserPolicy>({ registrationEnabled: true, maxUsers: 10000, defaultConcurrency: 3, maxAccountsPerTask: 100, allowFusion: true });
const policySaving = ref(false);
const loading = ref(false);
const error = ref("");
const notice = ref("");
const concurrency = ref<AdminConcurrency>({ default: 3, vip: 10, globalLimit: 100 });
const sharedConcurrency = computed(() => Math.max(1, Math.min(concurrency.value.default, concurrency.value.globalLimit, 100)));
const proxyPools = ref<AdminProxyPools>(emptyProxyPools());
const selectedProxyMode = ref("paypal");
const announcement = ref<AdminAnnouncement>({ title: "系统公告", content: "" });
const logs = ref<AdminLog[]>([]);
const routeStats = ref<AdminRouteStat[]>([]);
const announcementLoading = ref(false);
const activeSection = ref(new URLSearchParams(window.location.search).get("section") || "dashboard");
const adminSections = [
  { id: "dashboard", label: "仪表盘" }, { id: "concurrency", label: "并发配置" }, { id: "proxies", label: "代理池" },
  { id: "announcement", label: "公告管理" }, { id: "cards", label: "卡密与融合" }, { id: "routes", label: "路由统计" },
  { id: "logs", label: "提链日志" }, { id: "tasks", label: "任务记录" }, { id: "users", label: "用户列表" }, { id: "user-policy", label: "用户策略" }, { id: "danger", label: "危险操作" },
];
const sectionMeta: Record<string, { title: string; description: string }> = {
  dashboard: { title: "系统运行概览", description: "任务、卡密与基础设施状态集中管理。" },
  concurrency: { title: "并发配置", description: "统一设置普通用户的任务并发上限，保存后用户工作台自动更新，新任务使用最新配置。" },
  proxies: { title: "代理池管理", description: "按通道维护建单代理和出口代理，用户任务只读取可用状态。" },
  announcement: { title: "公告管理", description: "发布、更新或撤下用户工作台顶部公告。" },
  cards: { title: "卡密与融合", description: "批量制卡、融合、复制和管理 CDK 库存。" },
  routes: { title: "路由统计", description: "查看各支付通道的任务量、成功率和执行中数量。" },
  logs: { title: "提链日志", description: "查看任务执行和后台操作产生的审计日志。" },
  tasks: { title: "任务记录", description: "查看最近异步任务、任务项和最终状态。" },
  users: { title: "用户列表", description: "查看用户任务归属、在线状态和最后活跃时间。" },
  "user-policy": { title: "用户策略", description: "控制注册开关和新用户权限；所有普通用户的并发统一在并发配置中设置。" },
  danger: { title: "危险操作", description: "清理历史事件和统计数据，操作前请确认影响范围。" },
};
const activeSectionMeta = computed(() => sectionMeta[activeSection.value] || sectionMeta.dashboard);
const userPageCount = computed(() => Math.max(1, Math.ceil(usersTotal.value / userPageSize.value)));
const filteredCards = computed(() => cards.value.filter((card) => {
  const query = cardQuery.value.trim().toLowerCase();
  return (!query || [card.code, card.codeMask, card.remark, card.ownerUsername].filter(Boolean).some((item) => String(item).toLowerCase().includes(query)))
    && (cardStatus.value === "all" || card.status === cardStatus.value);
}));
const filteredTasks = computed(() => tasks.value.filter((task) => {
  const query = taskQuery.value.trim().toLowerCase();
  return (!query || [task.id, task.cardCode, task.ownerMask, task.modeName, task.mode].filter(Boolean).some((item) => String(item).toLowerCase().includes(query)))
    && (taskStatus.value === "all" || task.status === taskStatus.value)
    && (taskMode.value === "all" || task.mode === taskMode.value);
}));
const filteredLogs = computed(() => logs.value.filter((row) => {
  const query = logQuery.value.trim().toLowerCase();
  return (!query || [row.action, row.message, row.mode, row.taskId].filter(Boolean).some((item) => String(item).toLowerCase().includes(query)))
    && (logLevel.value === "all" || row.level === logLevel.value);
}));
const filteredFusionRecords = computed(() => {
  const query = fusionQuery.value.trim().toLowerCase();
  if (!query) return fusionRecords.value;
  return fusionRecords.value.filter((row) => [row.id, row.newCode, row.remark, row.actor, ...row.sourceCodes].some((item) => item.toLowerCase().includes(query)));
});

function selectSection(section: string) {
  activeSection.value = section;
  window.history.replaceState({}, "", `/admin?section=${encodeURIComponent(section)}`);
}
const generateForm = ref({ count: 1, quota: 100, days: 30, remark: "", fusionDisabled: false });
const generatedCodes = ref<string[]>([]);
const fusionText = ref("");
const fusionRemark = ref("管理员融合");
const fusionRecords = ref<Array<{ id: string; newCode: string; sourceCodes: string[]; totalRemaining: number; remark: string; actor: string; createdAt: string }>>([]);
const fusionQuery = ref("");
const cardQuery = ref("");
const cardStatus = ref("all");
const taskQuery = ref("");
const taskStatus = ref("all");
const taskMode = ref("all");
const logQuery = ref("");
const logLevel = ref("all");

const authenticated = computed(() => Boolean(token.value && overview.value));
const infrastructure = computed(() => [
  { name: "PostgreSQL", ok: cluster.value?.database.startsWith("postgresql") ?? false, detail: cluster.value?.database || "未连接" },
  { name: "Redis", ok: cluster.value?.redisConfigured ?? false, detail: cluster.value?.redisConfigured ? "缓存已配置" : "未配置" },
  { name: "Kafka", ok: cluster.value?.kafkaConfigured ?? false, detail: cluster.value?.consumerGroup || "未配置" },
]);

function statusLabel(status: string) {
  return ({ queued: "排队中", running: "执行中", success: "成功", failed: "失败", canceled: "已取消", active: "正常", disabled: "停用", expired: "过期", fused: "已融合" } as Record<string, string>)[status] || status;
}

function showError(value: unknown) {
  error.value = value instanceof Error ? value.message : "请求失败";
  notice.value = "";
}

async function copyText(value: string, message = "已复制") {
  try {
    await navigator.clipboard.writeText(value);
    notice.value = message;
    error.value = "";
  } catch {
    showError(new Error("复制失败，请允许浏览器访问剪贴板后重试"));
  }
}

async function copyGeneratedCodes() {
  if (!generatedCodes.value.length) return;
  await copyText(generatedCodes.value.join("\n"), `已复制 ${generatedCodes.value.length} 张卡密`);
}

async function loadAdminData() {
  if (!token.value) return;
  loading.value = true;
  error.value = "";
  try {
    const [overviewResult, clusterResult, cardsResult, tasksResult, usersResult, concurrencyResult, proxyResult, announcementResult, logsResult, routeResult, fusionResult, policyResult] = await Promise.all([
      api.adminOverview(token.value),
      api.adminCluster(token.value),
      api.adminCards(token.value),
      api.adminTasks(token.value),
      api.adminUsersFiltered(token.value, { q: userQuery.value, status: userStatus.value, limit: userPageSize.value, offset: (userPage.value - 1) * userPageSize.value }),
      api.adminConcurrency(token.value),
      api.adminProxies(token.value),
      api.adminAnnouncement(token.value),
      api.adminLogs(token.value),
      api.adminRouteStats(token.value),
      api.adminCardFusions(token.value),
      api.adminUserPolicy(token.value),
    ]);
    overview.value = overviewResult;
    cluster.value = clusterResult;
    cards.value = cardsResult.items;
    tasks.value = tasksResult.items;
    users.value = usersResult.items;
    usersTotal.value = usersResult.total;
    userPolicy.value = policyResult;
    concurrency.value = concurrencyResult;
    proxyPools.value = proxyResult;
    if (announcementResult.announcement) announcement.value = announcementResult.announcement;
    logs.value = logsResult.logs;
    routeStats.value = routeResult.items;
    fusionRecords.value = fusionResult.records;
  } catch (value) {
    if (value instanceof ApiError && value.status === 401) logout();
    showError(value);
  } finally {
    loading.value = false;
  }
}

async function loadUsers() {
  if (!token.value) return;
  usersLoading.value = true;
  try {
    const result = await api.adminUsersFiltered(token.value, { q: userQuery.value, status: userStatus.value, limit: userPageSize.value, offset: (userPage.value - 1) * userPageSize.value });
    users.value = result.items;
    usersTotal.value = result.total;
  } catch (value) {
    if (value instanceof ApiError && value.status === 401) logout();
    showError(value);
  } finally {
    usersLoading.value = false;
  }
}

function openUserEditor(user: AdminUser) {
  selectedUser.value = user;
  userDraft.value = {
    displayName: user.displayName,
    status: user.status,
    maxAccountsPerTask: user.maxAccountsPerTask,
    allowFusion: user.allowFusion,
    notes: user.notes || "",
  };
  userEditorOpen.value = true;
}

async function saveUser() {
  if (!selectedUser.value) return;
  userSaving.value = true;
  try {
    await api.updateAdminUser(token.value, selectedUser.value.id, userDraft.value);
    notifyRuntimeSettingsChanged();
    userEditorOpen.value = false;
    notice.value = `用户 ${selectedUser.value.username} 的配置已保存`;
    await loadUsers();
  } catch (value) { showError(value); } finally { userSaving.value = false; }
}

async function saveUserPolicy() {
  policySaving.value = true;
  try {
    userPolicy.value = await api.updateAdminUserPolicy(token.value, userPolicy.value);
    notifyRuntimeSettingsChanged();
    notice.value = "用户策略已保存，新注册用户会使用最新默认值";
  } catch (value) { showError(value); } finally { policySaving.value = false; }
}

function changeUserPage(delta: number) {
  userPage.value = Math.min(userPageCount.value, Math.max(1, userPage.value + delta));
  void loadUsers();
}

async function generateCards() {
  try { const result = await api.generateAdminCards(token.value, generateForm.value); generatedCodes.value = result.cards.map((item) => item.code); notice.value = `已生成 ${result.created} 张 CDK，请立即保存明文`; await loadAdminData(); } catch (value) { showError(value); }
}

async function fuseAdminCards() {
  const codes = fusionText.value.split(/[\s,，;；]+/).map((item) => item.trim().toUpperCase()).filter(Boolean);
  if (codes.length < 2) return showError(new Error("至少输入 2 个 CDK"));
  try { const result = await api.adminFuseCards(token.value, codes, fusionRemark.value); generatedCodes.value = [result.card.code]; fusionText.value = ""; notice.value = "融合成功，请立即保存新 CDK"; await loadAdminData(); } catch (value) { showError(value); }
}

async function saveConcurrency() {
  try {
    concurrency.value = await api.updateAdminConcurrency(token.value, concurrency.value);
    notifyRuntimeSettingsChanged();
    notice.value = "并发配置已保存，用户工作台会自动更新上限，新任务使用最新配置";
    error.value = "";
  } catch (value) { showError(value); }
}

async function saveProxyPools() {
  const selected = proxyPools.value.channels[selectedProxyMode.value];
  if (selected?.enabled && !selected.primaryText.trim()) return showError(new Error(`${proxyModeNames[selectedProxyMode.value]} 已启用，建单代理池不能为空`));
  try {
    proxyPools.value = await api.updateAdminProxies(token.value, proxyPools.value);
    notice.value = "代理池配置已保存，新任务会使用最新配置";
    error.value = "";
  } catch (value) { showError(value); }
}

async function publishAnnouncement() {
  if (!announcement.value.content.trim()) return showError(new Error("公告内容不能为空"));
  announcementLoading.value = true;
  try { const result = await api.publishAdminAnnouncement(token.value, { title: announcement.value.title, content: announcement.value.content }); announcement.value = result.announcement; notice.value = "公告已发布"; } catch (value) { showError(value); } finally { announcementLoading.value = false; }
}

async function removeAnnouncement() {
  try { await api.removeAdminAnnouncement(token.value); announcement.value.content = ""; notice.value = "公告已撤下"; } catch (value) { showError(value); }
}

async function clearLogs() {
  try { const result = await api.clearAdminLogs(token.value); logs.value = []; notice.value = `已清理 ${result.deleted} 条日志`; } catch (value) { showError(value); }
}

async function resetStats() {
  if (!window.confirm("确认清空任务事件和审计统计？此操作不可恢复。")) return;
  try { await api.resetAdminStats(token.value); notice.value = "统计事件已清空"; await loadAdminData(); } catch (value) { showError(value); }
}

async function toggleCard(card: AdminCard) {
  try { await api.updateCardStatus(token.value, card.id, { status: card.status === "active" ? "disabled" : "active" }); await loadAdminData(); notice.value = "卡密状态已更新"; } catch (value) { showError(value); }
}

async function toggleCardFusion(card: AdminCard) {
  try {
    await api.updateCardStatus(token.value, card.id, { fusionDisabled: !card.fusionDisabled });
    await loadAdminData();
    notice.value = card.fusionDisabled ? "已允许该卡参与融合" : "已禁止该卡参与融合";
  } catch (value) { showError(value); }
}

async function login() {
  const value = tokenInput.value.trim();
  if (!value) return showError(new Error("请输入管理员密钥"));
  token.value = value;
  await loadAdminData();
  if (overview.value) {
    sessionStorage.setItem(TOKEN_KEY, value);
    tokenInput.value = "";
  }
}

function logout() {
  token.value = "";
  overview.value = null;
  cluster.value = null;
  cards.value = [];
  tasks.value = [];
  sessionStorage.removeItem(TOKEN_KEY);
}

watch([userQuery, userStatus, userPageSize], () => {
  userPage.value = 1;
  if (authenticated.value) void loadUsers();
});

onMounted(() => {
  if (token.value) void loadAdminData();
});
</script>

<template>
  <div v-if="!authenticated" class="admin-login-page">
    <section class="admin-login-panel">
      <span class="admin-login-icon"><ShieldCheck :size="26" /></span>
      <p class="admin-kicker">SECURE ADMIN ACCESS</p>
      <h1>管理控制台</h1>
      <p>管理员密钥仅保存在当前浏览器会话中。</p>
      <label for="adminToken">管理员密钥</label>
      <div class="admin-token-field">
        <KeyRound :size="17" />
        <input id="adminToken" v-model="tokenInput" type="password" autocomplete="off" placeholder="输入 ADMIN_API_TOKEN" @keyup.enter="login" />
      </div>
      <p v-if="error" class="admin-message error">{{ error }}</p>
      <button class="button primary admin-login-button" type="button" :disabled="loading" @click="login">
        <LoaderCircle v-if="loading" :size="17" class="spin" /><ShieldCheck v-else :size="17" />
        {{ loading ? '正在验证' : '进入管理后台' }}
      </button>
      <a class="admin-back" href="/"><ArrowLeft :size="15" />返回提链工作台</a>
    </section>
  </div>

  <div v-else class="admin-page">
    <header class="admin-topbar">
      <a class="admin-brand" href="/"><span><Layers3 :size="19" /></span><strong>DJB 管理控制台</strong></a>
      <div class="admin-top-actions">
        <span class="admin-online"><i />集群在线</span>
        <button class="icon-button" type="button" title="刷新数据" :disabled="loading" @click="loadAdminData"><RefreshCw :size="18" :class="{ spin: loading }" /></button>
        <button class="button secondary" type="button" @click="logout"><LogOut :size="16" />退出</button>
      </div>
    </header>

    <div class="admin-layout">
      <aside class="admin-sidebar">
        <div class="admin-sidebar-title">管理模块</div>
        <nav>
          <a v-for="item in adminSections" :key="item.id" :href="`/admin?section=${item.id}`" :class="{ active: activeSection === item.id }" @click.prevent="selectSection(item.id)">{{ item.label }}</a>
        </nav>
        <div class="admin-sidebar-note"><ShieldCheck :size="16" /><span>集群在线<br /><small>数据写入 PostgreSQL</small></span></div>
      </aside>
      <main class="admin-content">
      <section id="admin-dashboard" class="admin-heading">
        <div><p class="admin-kicker">OPERATIONS / DISTRIBUTED CLUSTER</p><h1>{{ activeSectionMeta.title }}</h1><p>{{ activeSectionMeta.description }}</p></div>
        <a class="button secondary" href="/"><ArrowLeft :size="16" />返回工作台</a>
      </section>

      <p v-if="error" class="admin-banner error"><XCircle :size="16" />{{ error }}</p>
      <p v-if="notice" class="admin-banner ok"><CheckCircle2 :size="16" />{{ notice }}</p>

      <section v-if="activeSection === 'dashboard'" id="admin-dashboard-metrics" class="admin-metrics">
        <article><span class="blue"><Activity :size="19" /></span><div><small>任务总数</small><strong>{{ overview?.tasks.total || 0 }}</strong><em>{{ overview?.tasks.running || 0 }} 个执行中</em></div></article>
        <article><span class="green"><CheckCircle2 :size="19" /></span><div><small>成功任务项</small><strong>{{ overview?.items.success || 0 }}</strong><em>共 {{ overview?.items.total || 0 }} 项</em></div></article>
        <article><span class="amber"><KeyRound :size="19" /></span><div><small>有效卡密</small><strong>{{ overview?.cards.active || 0 }}</strong><em>共 {{ overview?.cards.total || 0 }} 张</em></div></article>
        <article><span class="cyan"><Database :size="19" /></span><div><small>剩余额度</small><strong>{{ (overview?.cards.quota || 0) - (overview?.cards.used || 0) - (overview?.cards.reserved || 0) }}</strong><em>已用 {{ overview?.cards.used || 0 }}</em></div></article>
      </section>

      <section v-if="activeSection === 'dashboard'" id="admin-cluster" class="admin-panel">
        <div class="admin-panel-head"><div><span>CLUSTER HEALTH</span><h2>集群与中间件</h2></div><code>{{ cluster?.mode }}</code></div>
        <div class="infra-grid">
          <article v-for="item in infrastructure" :key="item.name">
            <span :class="{ ok: item.ok }"><Server :size="18" /></span>
            <div><strong>{{ item.name }}</strong><small>{{ item.detail }}</small></div>
            <em :class="{ ok: item.ok }">{{ item.ok ? '正常' : '待配置' }}</em>
          </article>
        </div>
        <div class="topic-line"><b>Kafka Topics</b><code>{{ cluster?.topics.commands }}</code><code>{{ cluster?.topics.events }}</code><code>{{ cluster?.topics.dlq }}</code></div>
      </section>

      <section v-if="activeSection === 'concurrency'" id="admin-concurrency" class="admin-grid admin-grid-single">
        <article class="admin-panel">
          <div class="admin-panel-head"><div><span>CONCURRENCY</span><h2>任务并发配置</h2><p>所有普通用户共用此上限；每个任务仍可选择更小的并发。</p></div><Route :size="19" /></div>
          <div class="settings-grid">
            <label>普通用户并发上限<input v-model.number="concurrency.default" type="number" min="1" max="100" /></label>
            <label>VIP 并发（预留）<input v-model.number="concurrency.vip" type="number" disabled /><small>用户分级尚未启用</small></label>
            <label>单任务硬上限<input v-model.number="concurrency.globalLimit" type="number" min="1" max="1000" /><small>每个任务最多同时执行的账号数</small></label>
          </div>
          <button class="button primary" type="button" @click="saveConcurrency"><Save :size="16" />保存并发配置</button>
        </article>
      </section>

      <section v-if="activeSection === 'announcement'" id="admin-announcement-page" class="admin-panel">
        <div class="admin-panel-head"><div><span>ANNOUNCEMENT</span><h2>系统公告</h2><p>发布后会立即显示在用户工作台顶部。</p></div><Megaphone :size="19" /></div>
        <input v-model="announcement.title" maxlength="160" placeholder="公告标题" />
        <textarea v-model="announcement.content" rows="8" maxlength="5000" placeholder="向用户展示的公告内容" />
        <div class="panel-actions"><button class="button primary" type="button" :disabled="announcementLoading" @click="publishAnnouncement"><Megaphone :size="16" />发布公告</button><button class="button ghost danger" type="button" @click="removeAnnouncement">撤下</button></div>
      </section>

      <section v-if="activeSection === 'proxies'" id="admin-proxies" class="admin-panel proxy-config-panel">
        <div class="admin-panel-head"><div><span>PROXY POOLS</span><h2>通道代理池</h2></div><Route :size="19" /></div>
        <div class="proxy-mode-tabs" role="tablist">
          <button v-for="(name, mode) in proxyModeNames" :key="mode" type="button" :class="{ active: selectedProxyMode === mode }" @click="selectedProxyMode = mode">{{ name }}<i :class="{ enabled: proxyPools.channels[mode]?.enabled }" /></button>
        </div>
        <div v-if="proxyPools.channels[selectedProxyMode]" class="proxy-editor">
          <label class="switch-control"><input v-model="proxyPools.channels[selectedProxyMode].enabled" type="checkbox" /><span class="switch-ui" aria-hidden="true" /><span class="switch-copy"><b>启用 {{ proxyModeNames[selectedProxyMode] }} 管理员代理池</b><small>用户选择管理员代理池时，将配置快照写入新任务</small></span></label>
          <label><span>建单代理池 <small>{{ proxyPools.channels[selectedProxyMode].primaryText.split(/\r?\n/).filter(Boolean).length }} 条</small></span><textarea v-model="proxyPools.channels[selectedProxyMode].primaryText" rows="7" placeholder="每行一个 HTTP / HTTPS / SOCKS5 代理" /></label>
          <label><span>出口 / 账单代理池 <small>{{ proxyPools.channels[selectedProxyMode].exitText.split(/\r?\n/).filter(Boolean).length }} 条</small></span><textarea v-model="proxyPools.channels[selectedProxyMode].exitText" rows="7" placeholder="可选；留空时使用建单代理" /></label>
        </div>
        <div class="proxy-save-line"><p class="panel-copy">代理地址只通过管理员接口读取，普通用户只能看到可用状态和数量。</p><button class="button primary" type="button" @click="saveProxyPools"><Save :size="16" />保存代理池</button></div>
      </section>

      <section v-if="activeSection === 'routes'" id="admin-routes" class="admin-panel">
        <div class="admin-panel-head"><div><span>ROUTE STATISTICS</span><h2>通道路由统计</h2></div><Route :size="19" /></div>
        <div class="admin-table-wrap compact"><table><thead><tr><th>通道</th><th>总任务</th><th>成功</th><th>失败</th><th>执行中</th></tr></thead><tbody><tr v-for="item in routeStats" :key="item.mode"><td>{{ item.mode }}</td><td>{{ item.total }}</td><td>{{ item.success }}</td><td>{{ item.failed }}</td><td>{{ item.running }}</td></tr><tr v-if="!routeStats.length"><td colspan="5" class="table-empty">暂无路由数据</td></tr></tbody></table></div>
      </section>

      <section v-if="activeSection === 'cards'" id="admin-cards" class="cards-management">
        <article class="admin-panel card-issue-panel">
          <div class="admin-panel-head"><div><span>CARD ISSUE</span><h2>批量制卡</h2><p>生成的明文只在当前结果区保留，支持逐张复制或一次复制全部。</p></div><KeyRound :size="19" /></div>
          <div class="settings-grid"><label>数量<input v-model.number="generateForm.count" type="number" min="1" max="500" /></label><label>每张额度<input v-model.number="generateForm.quota" type="number" min="1" /></label><label>有效期（天）<input v-model.number="generateForm.days" type="number" min="1" /></label></div>
          <input v-model="generateForm.remark" placeholder="制卡备注（可选）" />
          <label class="switch-control"><input v-model="generateForm.fusionDisabled" type="checkbox" /><span class="switch-ui" aria-hidden="true" /><span class="switch-copy"><b>禁止融合</b><small>打开后，这批卡只能校验和提链，不能作为融合来源。</small></span></label>
          <button class="button primary action-button" type="button" @click="generateCards"><KeyRound :size="16" />生成卡密</button>
          <div v-if="generatedCodes.length" class="generated-codes">
            <div class="generated-codes-head"><span><strong>本次生成结果</strong><small>请立即复制保存，离开页面后不再重复显示。</small></span><button class="button secondary" type="button" @click="copyGeneratedCodes"><Copy :size="14" />复制全部</button></div>
            <div v-for="code in generatedCodes" :key="code" class="generated-code-row"><code>{{ code }}</code><button class="copy-button" type="button" @click="copyText(code, '卡密已复制')"><Copy :size="12" />复制</button></div>
          </div>
        </article>

        <article class="admin-panel card-fusion-panel">
          <div class="admin-panel-head"><div><span>CARD FUSION</span><h2>CDK 融合</h2><p>每行一个来源 CDK，来源卡会失效，新卡额度为来源卡剩余额度之和。</p></div><Layers3 :size="19" /></div>
          <textarea v-model="fusionText" rows="6" placeholder="输入至少 2 个 CDK，可换行、空格或逗号分隔" />
          <input v-model="fusionRemark" maxlength="160" placeholder="融合备注（可选）" />
          <button class="button primary action-button" type="button" @click="fuseAdminCards"><Layers3 :size="16" />确认融合</button>
          <p class="panel-copy">来源 CDK 不能有执行中的任务；禁融卡、停用卡和其他用户绑定的卡会被拒绝。</p>
          <div class="fusion-records-head"><div><strong>融合记录</strong><small>{{ filteredFusionRecords.length }} / {{ fusionRecords.length }} 条</small></div><label class="search-field"><Search :size="14" /><input v-model="fusionQuery" placeholder="搜索新卡、来源卡或备注" /></label><button class="icon-button" type="button" title="刷新融合记录" :disabled="loading" @click="loadAdminData"><RefreshCw :size="16" :class="{ spin: loading }" /></button></div>
          <div class="admin-table-wrap fusion-records-table"><table><thead><tr><th>时间</th><th>新 CDK</th><th>来源 CDK</th><th>额度</th><th>操作人</th></tr></thead><tbody><tr v-for="row in filteredFusionRecords" :key="row.id"><td>{{ new Date(row.createdAt).toLocaleString() }}</td><td><div class="code-with-action"><code>{{ row.newCode }}</code><button class="copy-button" type="button" @click="copyText(row.newCode, '融合新卡已复制')"><Copy :size="12" />复制</button></div></td><td><div class="source-codes"><span v-for="source in row.sourceCodes" :key="source"><code>{{ source }}</code></span><button class="copy-button" type="button" @click="copyText(row.sourceCodes.join('\n'), '来源卡已复制')"><Copy :size="12" />复制来源</button></div></td><td>{{ row.totalRemaining }}</td><td>{{ row.actor === 'admin' ? '管理员' : row.actor }}</td></tr><tr v-if="!filteredFusionRecords.length"><td colspan="5" class="table-empty">没有符合条件的融合记录</td></tr></tbody></table></div>
        </article>

        <article class="admin-panel card-inventory-panel">
          <div class="admin-panel-head"><div><span>CARD INVENTORY</span><h2>卡密库存</h2><p>明文展示卡密、绑定用户和额度，支持快速定位和停用。</p></div><b>{{ filteredCards.length }} / {{ cards.length }} 张</b></div>
          <div class="data-toolbar"><label class="search-field"><Search :size="15" /><input v-model="cardQuery" placeholder="搜索卡密、备注或用户" /></label><label class="select-field"><Filter :size="14" /><select v-model="cardStatus"><option value="all">全部状态</option><option value="active">正常</option><option value="disabled">停用</option><option value="expired">过期</option><option value="fused">已融合</option></select></label></div>
          <div class="admin-table-wrap compact">
            <table><thead><tr><th>卡密</th><th>绑定用户</th><th>额度</th><th>使用</th><th>剩余</th><th>融合</th><th>状态</th></tr></thead>
            <tbody><tr v-for="card in filteredCards" :key="card.id"><td><code>{{ card.code || card.codeMask }}</code><small v-if="card.remark" class="cell-subtitle">{{ card.remark }}</small><small v-if="card.boundAt" class="cell-subtitle">绑定于 {{ new Date(card.boundAt).toLocaleString() }}</small></td><td>{{ card.ownerUsername || (card.ownerUserId ? `用户 #${card.ownerUserId}` : '未绑定') }}</td><td>{{ card.quota }}</td><td>{{ card.usedQuota }}</td><td class="remain-cell">{{ card.remain }}</td><td><span class="status" :class="card.fusionDisabled ? 'disabled' : 'active'">{{ card.fusionDisabled ? '禁止' : '允许' }}</span><button v-if="card.status === 'active'" class="mini-action" type="button" @click="toggleCardFusion(card)">{{ card.fusionDisabled ? '允许融合' : '禁用融合' }}</button></td><td><span class="status" :class="card.status">{{ statusLabel(card.status) }}</span> <button v-if="card.status !== 'fused'" class="mini-action" type="button" @click="toggleCard(card)">{{ card.status === 'active' ? '禁用' : '启用' }}</button></td></tr>
              <tr v-if="!filteredCards.length"><td colspan="7" class="table-empty">没有符合筛选条件的卡密</td></tr></tbody>
            </table>
          </div>
        </article>
      </section>

      <section v-if="activeSection === 'logs'" id="admin-logs" class="admin-panel">
        <div class="admin-panel-head"><div><span>OPERATION LOGS</span><h2>提链与审计日志</h2><p>日志默认按最新时间排列，可按级别、动作和任务快速定位。</p></div><div class="panel-actions"><b>{{ filteredLogs.length }} / {{ logs.length }} 条</b><button class="button ghost danger" type="button" @click="clearLogs"><ScrollText :size="15" />清空日志</button></div></div>
        <div class="data-toolbar"><label class="search-field"><Search :size="15" /><input v-model="logQuery" placeholder="搜索动作、说明或任务" /></label><label class="select-field"><Filter :size="14" /><select v-model="logLevel"><option value="all">全部级别</option><option value="info">info</option><option value="warning">warning</option><option value="error">error</option></select></label></div>
        <div class="admin-table-wrap"><table><thead><tr><th>时间</th><th>级别</th><th>动作</th><th>说明</th><th>通道</th><th>任务</th></tr></thead><tbody><tr v-for="row in filteredLogs" :key="row.id"><td>{{ new Date(row.createdAt).toLocaleString() }}</td><td><span class="status" :class="row.level">{{ row.level }}</span></td><td>{{ row.action }}</td><td>{{ row.message }}</td><td>{{ row.mode || '-' }}</td><td><code>{{ row.taskId || '-' }}</code></td></tr><tr v-if="!filteredLogs.length"><td colspan="6" class="table-empty">没有符合筛选条件的日志</td></tr></tbody></table></div>
      </section>

      <section v-if="activeSection === 'danger'" id="admin-danger" class="admin-panel danger-zone">
        <div class="admin-panel-head"><div><span>DANGER ZONE</span><h2>危险操作</h2></div><TriangleAlert :size="19" /></div>
        <p class="panel-copy">清理事件和统计数据不会删除卡密，但会影响后台历史统计展示。</p>
        <button class="button ghost danger" type="button" @click="resetStats"><TriangleAlert :size="16" />清空统计事件</button>
      </section>

      <section v-if="activeSection === 'tasks'" id="admin-tasks" class="admin-panel">
        <div class="admin-panel-head"><div><span>ASYNC TASKS</span><h2>最近任务</h2><p>按任务 ID、卡密、通道或状态筛选异步执行记录。</p></div><b>{{ filteredTasks.length }} / {{ tasks.length }} 条</b></div>
        <div class="data-toolbar"><label class="search-field"><Search :size="15" /><input v-model="taskQuery" placeholder="搜索任务、卡密或通道" /></label><label class="select-field"><Filter :size="14" /><select v-model="taskStatus"><option value="all">全部状态</option><option value="queued">排队中</option><option value="running">执行中</option><option value="success">成功</option><option value="failed">失败</option><option value="canceled">已取消</option></select></label><label class="select-field"><select v-model="taskMode"><option value="all">全部通道</option><option v-for="(name, mode) in proxyModeNames" :key="mode" :value="mode">{{ name }}</option></select></label></div>
        <div class="admin-table-wrap">
          <table><thead><tr><th>任务 ID</th><th>通道</th><th>卡密</th><th>任务项</th><th>成功 / 失败</th><th>并发</th><th>状态</th><th>创建时间</th></tr></thead>
            <tbody><tr v-for="task in filteredTasks" :key="task.id"><td><code>{{ task.id }}</code></td><td>{{ task.modeName || task.mode }}</td><td><code>{{ task.cardCode || task.ownerMask }}</code></td><td>{{ task.itemCount }}</td><td>{{ task.successCount }} / {{ task.failedCount }}</td><td>{{ task.concurrency }}</td><td><span class="status" :class="task.status">{{ statusLabel(task.status) }}</span></td><td>{{ new Date(task.createdAt).toLocaleString() }}</td></tr>
            <tr v-if="!filteredTasks.length"><td colspan="8" class="table-empty">没有符合筛选条件的任务</td></tr></tbody>
          </table>
        </div>
      </section>
      <section v-if="activeSection === 'users'" id="admin-users" class="admin-panel">
        <div class="admin-panel-head"><div><span>USERS</span><h2>用户列表</h2><p>搜索用户名、显示名称或管理员备注，点击编辑可调整权限和任务限制。</p></div><b>{{ usersTotal }} 个</b></div>
        <div class="data-toolbar user-toolbar"><label class="search-field"><Search :size="15" /><input v-model="userQuery" placeholder="搜索用户名、昵称或备注" /></label><label class="select-field"><Filter :size="14" /><select v-model="userStatus"><option value="all">全部状态</option><option value="active">正常</option><option value="disabled">已停用</option></select></label><label class="select-field"><select v-model.number="userPageSize"><option :value="10">每页 10 条</option><option :value="20">每页 20 条</option><option :value="50">每页 50 条</option></select></label></div>
        <div class="admin-table-wrap"><table><thead><tr><th>用户</th><th>状态</th><th>任务 / 卡密</th><th>并发</th><th>单任务账号</th><th>融合</th><th>最后登录</th><th>操作</th></tr></thead><tbody><tr v-for="user in users" :key="user.id"><td><div class="user-cell"><span class="user-avatar"><UserRound :size="15" /></span><span><strong>{{ user.displayName }}</strong><small>@{{ user.username }} · ID {{ user.id }}</small></span></div></td><td><span class="status" :class="user.status">{{ user.status === 'active' ? '正常' : '已停用' }}</span><small v-if="user.online" class="online-dot">● 在线</small></td><td>{{ user.taskCount }} / {{ user.cardCount }}</td><td>{{ user.taskConcurrency }}</td><td>{{ user.maxAccountsPerTask }}</td><td><span class="status" :class="user.allowFusion ? 'active' : 'disabled'">{{ user.allowFusion ? '允许' : '禁止' }}</span></td><td>{{ user.lastLoginAt ? new Date(user.lastLoginAt).toLocaleString() : '从未登录' }}</td><td><button class="mini-action" type="button" @click="openUserEditor(user)"><Pencil :size="12" />编辑</button></td></tr><tr v-if="!users.length"><td colspan="8" class="table-empty">没有符合筛选条件的用户</td></tr></tbody></table></div>
        <div class="pager"><span>第 {{ userPage }} / {{ userPageCount }} 页，共 {{ usersTotal }} 个用户</span><div><button class="icon-button" type="button" :disabled="userPage <= 1 || usersLoading" @click="changeUserPage(-1)"><ChevronLeft :size="16" /></button><button class="icon-button" type="button" :disabled="userPage >= userPageCount || usersLoading" @click="changeUserPage(1)"><ChevronRight :size="16" /></button></div></div>
      </section>
      <section v-if="activeSection === 'user-policy'" id="admin-user-policy" class="admin-grid admin-grid-single">
        <article class="admin-panel policy-panel">
          <div class="admin-panel-head"><div><span>USER POLICY</span><h2>注册与默认权限</h2><p>这些配置用于新用户注册；已存在用户请在用户列表中单独编辑。</p></div><Settings2 :size="19" /></div>
          <label class="switch-control"><input v-model="userPolicy.registrationEnabled" type="checkbox" /><span class="switch-ui" aria-hidden="true" /><span class="switch-copy"><b>开放用户注册</b><small>关闭后，已有用户仍可登录和使用工作台。</small></span><Check v-if="userPolicy.registrationEnabled" :size="17" /></label>
          <label class="switch-control"><input v-model="userPolicy.allowFusion" type="checkbox" /><span class="switch-ui" aria-hidden="true" /><span class="switch-copy"><b>新用户默认允许 CDK 融合</b><small>用户仍只能融合未绑定或自己绑定的 CDK。</small></span><Check v-if="userPolicy.allowFusion" :size="17" /></label>
          <div class="settings-grid policy-settings"><label>最大注册用户数<input v-model.number="userPolicy.maxUsers" type="number" min="1" max="10000000" /><small>达到上限后停止新注册</small></label><label>统一并发上限<input :value="sharedConcurrency" type="number" readonly /><small>在“并发配置”中修改，对所有普通用户生效</small></label><label>单任务最大账号数<input v-model.number="userPolicy.maxAccountsPerTask" type="number" min="1" max="1000" /><small>限制一次提交的账号数量</small></label></div>
          <button class="button primary" type="button" :disabled="policySaving" @click="saveUserPolicy"><LoaderCircle v-if="policySaving" :size="16" class="spin" /><Save v-else :size="16" />{{ policySaving ? '正在保存' : '保存用户策略' }}</button>
        </article>
      </section>
      </main>
    </div>
    <transition name="modal">
      <div v-if="userEditorOpen && selectedUser" class="modal-backdrop admin-modal-backdrop" @click.self="userEditorOpen = false">
        <section class="modal-panel admin-user-modal">
          <div class="admin-panel-head"><div><span>USER SETTINGS</span><h2>编辑用户</h2><p>@{{ selectedUser.username }} · 注册于 {{ new Date(selectedUser.createdAt).toLocaleDateString() }}</p></div><button class="icon-button" type="button" title="关闭" @click="userEditorOpen = false">×</button></div>
          <div class="user-edit-grid">
            <label>显示名称<input v-model="userDraft.displayName" maxlength="120" /></label>
            <label>账号状态<select v-model="userDraft.status"><option value="active">正常</option><option value="disabled">停用并撤销登录</option></select></label>
            <label>统一并发上限<input :value="sharedConcurrency" type="number" readonly /><small>跟随“并发配置”，无需单独设置</small></label>
            <label>单任务账号上限<input v-model.number="userDraft.maxAccountsPerTask" type="number" min="1" max="1000" /></label>
          </div>
          <label class="switch-control"><input v-model="userDraft.allowFusion" type="checkbox" /><span class="switch-ui" aria-hidden="true" /><span class="switch-copy"><b>允许 CDK 融合</b><small>仅影响该用户；CDK 归属规则仍然生效。</small></span><Check v-if="userDraft.allowFusion" :size="17" /></label>
          <label class="edit-notes">管理员备注<textarea v-model="userDraft.notes" rows="4" maxlength="500" placeholder="记录用户来源、套餐或处理说明" /></label>
          <div class="modal-actions"><button class="button secondary" type="button" @click="userEditorOpen = false">取消</button><button class="button primary" type="button" :disabled="userSaving" @click="saveUser"><LoaderCircle v-if="userSaving" :size="16" class="spin" /><Save v-else :size="16" />{{ userSaving ? '正在保存' : '保存用户配置' }}</button></div>
        </section>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.admin-login-page { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #111218; }
.admin-login-panel { width: min(410px, 100%); padding: 32px; background: #1b1e27; border: 1px solid #303544; border-radius: 7px; box-shadow: 0 24px 70px rgba(0,0,0,.38); }
.admin-login-icon { width: 48px; height: 48px; display: grid; place-items: center; color: #73aaff; background: #17243a; border: 1px solid #2b4671; border-radius: 7px; }
.admin-kicker { margin: 16px 0 7px; color: #5799ff !important; font-size: 10px !important; font-weight: 800; letter-spacing: .8px; }
.admin-login-panel h1, .admin-heading h1 { margin: 0; font-size: 27px; }
.admin-login-panel > p { color: #9099aa; font-size: 12px; line-height: 1.6; }
.admin-login-panel label { display: block; margin: 22px 0 7px; color: #aab2c1; font-size: 11px; }
.admin-token-field { display: flex; align-items: center; gap: 8px; padding-left: 11px; color: #697285; background: #151820; border: 1px solid #303544; border-radius: 6px; }
.admin-token-field input { border: 0; background: transparent; box-shadow: none; }
.admin-login-button { width: 100%; margin-top: 10px; }
.admin-back { display: flex; align-items: center; justify-content: center; gap: 6px; margin-top: 18px; color: #9099aa; font-size: 11px; text-decoration: none; }
.admin-message.error { color: #ef6574; }
.admin-page { min-height: 100vh; background: #111218; }
.admin-topbar { position: sticky; z-index: 20; top: 0; height: 60px; display: flex; align-items: center; justify-content: space-between; padding: 0 24px; background: rgba(20,22,30,.96); border-bottom: 1px solid #272b36; backdrop-filter: blur(14px); }
.admin-brand { display: flex; align-items: center; gap: 10px; color: #f1f3f7; text-decoration: none; }
.admin-brand span { width: 30px; height: 30px; display: grid; place-items: center; color: #73aaff; background: #17243a; border: 1px solid #2b4671; border-radius: 6px; }
.admin-brand strong { font-size: 14px; }
.admin-top-actions { display: flex; align-items: center; gap: 8px; }
.admin-online { display: flex; align-items: center; gap: 7px; margin-right: 8px; color: #aab2c1; font-size: 11px; }
.admin-online i { width: 6px; height: 6px; background: #30c991; border-radius: 50%; box-shadow: 0 0 8px #30c991; }
.admin-layout { max-width: 1540px; margin: 0 auto; display: grid; grid-template-columns: 190px minmax(0, 1fr); align-items: start; gap: 0; }.admin-sidebar { position: sticky; top: 60px; height: calc(100vh - 60px); padding: 24px 12px; background: #15171f; border-right: 1px solid #272b36; }.admin-sidebar-title { padding: 0 10px 12px; color: #697285; font-size: 10px; font-weight: 800; letter-spacing: .5px; }.admin-sidebar nav { display: grid; gap: 3px; }.admin-sidebar nav a { display: block; padding: 9px 10px; color: #9099aa; border-left: 2px solid transparent; border-radius: 4px; font-size: 11px; text-decoration: none; }.admin-sidebar nav a:hover { color: #fff; background: #1c202a; }.admin-sidebar nav a.active { color: #fff; background: #1d2940; border-left-color: #3488ff; }.admin-sidebar-note { display: flex; gap: 8px; margin: 22px 4px 0; padding: 10px; color: #30c991; background: rgba(48,201,145,.06); border: 1px solid rgba(48,201,145,.16); border-radius: 5px; font-size: 10px; }.admin-sidebar-note span { color: #b7bfcd; }.admin-sidebar-note small { color: #697285; font-size: 9px; }.admin-content { min-width: 0; padding: 28px 30px 50px; }
.admin-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 24px; }
.admin-heading .admin-kicker { margin-top: 0; }
.admin-heading p:last-child { margin: 7px 0 0; color: #9099aa; font-size: 12px; }
.admin-banner { display: flex; align-items: center; gap: 8px; padding: 10px 12px; font-size: 11px; border: 1px solid; border-radius: 6px; }
.admin-page .button { min-height: 38px; padding: 0 15px; border-radius: 9px; font-size: 11px; font-weight: 650; letter-spacing: .05px; box-shadow: 0 3px 10px rgba(0,0,0,.12); }
.admin-page .button.primary { color: #fff; background: linear-gradient(135deg, #3488ff, #2566c8); border-color: #5799ff; box-shadow: 0 6px 16px rgba(52,136,255,.2); }
.admin-page .button.primary:hover:not(:disabled) { background: linear-gradient(135deg, #4a98ff, #2e72d4); box-shadow: 0 9px 22px rgba(52,136,255,.3); transform: translateY(-2px); }
.admin-page .button.secondary { color: #d8e5ff; background: linear-gradient(180deg, #26344d, #202938); border-color: #3c5880; }
.admin-page .button.secondary:hover:not(:disabled) { background: #2b4162; border-color: #5a82ba; }
.admin-page .button.ghost { color: #aab2c1; background: transparent; box-shadow: none; }
.admin-page .button.ghost:hover:not(:disabled) { color: #fff; background: #202531; }
.admin-page .icon-button { width: 36px; height: 36px; border: 1px solid #303b50; border-radius: 9px; background: #1b2332; color: #a8c6f4; }
.admin-page .icon-button:hover:not(:disabled) { color: #fff; background: #263b60; border-color: #5799ff; transform: translateY(-1px); }
.admin-banner.error { color: #ff9ba6; background: rgba(239,101,116,.08); border-color: rgba(239,101,116,.3); }
.admin-banner.ok { color: #7bdbba; background: rgba(48,201,145,.07); border-color: rgba(48,201,145,.26); }
.admin-metrics { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 12px; margin-bottom: 16px; }
.admin-metrics article { display: flex; align-items: center; gap: 13px; padding: 17px; background: #1b1e27; border: 1px solid #272b36; border-radius: 7px; }
.admin-metrics article > span { width: 38px; height: 38px; flex: none; display: grid; place-items: center; color: #3488ff; background: rgba(52,136,255,.1); border-radius: 6px; }
.admin-metrics article > span.green { color: #30c991; background: rgba(48,201,145,.1); }.admin-metrics article > span.amber { color: #f3ad4d; background: rgba(243,173,77,.1); }.admin-metrics article > span.cyan { color: #24c9d7; background: rgba(36,201,215,.1); }
.admin-metrics article div { min-width: 0; display: grid; grid-template-columns: auto 1fr; align-items: baseline; gap: 3px 8px; }.admin-metrics small { grid-column: 1/-1; color: #9099aa; font-size: 11px; }.admin-metrics strong { font-size: 22px; }.admin-metrics em { color: #697285; font-size: 10px; font-style: normal; }
.admin-panel { padding: 18px; background: #1b1e27; border: 1px solid #272b36; border-radius: 7px; }.admin-panel + .admin-panel { margin-top: 0; }
.admin-panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 15px; }.admin-panel-head span { color: #5799ff; font-size: 9px; font-weight: 800; letter-spacing: .8px; }.admin-panel-head h2 { margin: 4px 0 0; font-size: 15px; }.admin-panel-head > code, .admin-panel-head > b { color: #9099aa; font-size: 10px; font-weight: 500; }
.infra-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 10px; }.infra-grid article { display: grid; grid-template-columns: 34px 1fr auto; align-items: center; gap: 10px; padding: 12px; background: #181b23; border: 1px solid #272b36; border-radius: 6px; }.infra-grid article > span { width: 34px; height: 34px; display: grid; place-items: center; color: #697285; background: #20242f; border-radius: 5px; }.infra-grid article > span.ok { color: #30c991; }.infra-grid article div { min-width: 0; display: grid; gap: 3px; }.infra-grid strong { font-size: 12px; }.infra-grid small { overflow: hidden; color: #697285; font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }.infra-grid em { color: #697285; font-size: 9px; font-style: normal; }.infra-grid em.ok { color: #30c991; }
.topic-line { display: flex; align-items: center; gap: 8px; margin-top: 12px; overflow-x: auto; color: #697285; font-size: 9px; }.topic-line b { color: #9099aa; white-space: nowrap; }.topic-line code { padding: 4px 7px; background: #151820; border: 1px solid #272b36; border-radius: 4px; white-space: nowrap; }
.admin-grid { display: grid; grid-template-columns: minmax(300px,.75fr) minmax(0,1.25fr); gap: 16px; margin: 16px 0; }.admin-grid-single { grid-template-columns: minmax(0,760px); }.cards-management { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 16px 0; }.cards-management > .card-inventory-panel { grid-column: 1 / -1; }.card-issue-panel, .card-fusion-panel, .card-inventory-panel { min-width: 0; }.card-fusion-panel { display: flex; flex-direction: column; align-items: stretch; gap: 10px; }.card-fusion-panel > .panel-copy { margin: 0; padding: 1px 0 2px; }.panel-copy { margin: -7px 0 12px; color: #9099aa; font-size: 10px; }.admin-panel textarea { margin-bottom: 10px; }
.settings-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }.settings-grid label { display: grid; gap: 6px; color: #9099aa; font-size: 10px; }.settings-grid input { width: 100%; }.mini-action { margin-left: 5px; padding: 5px 8px; color: #a6c8ff; background: #1d2940; border: 1px solid #385884; border-radius: 6px; font-size: 9px; cursor: pointer; transition: color .16s ease, background .16s ease, border-color .16s ease, transform .16s ease; }.mini-action:hover { color: #fff; background: #2b4773; border-color: #6aa4ff; transform: translateY(-1px); }.danger-zone { border-color: rgba(239,101,116,.35); }.status.info { color: #73aaff; background: rgba(52,136,255,.1); }
.proxy-mode-tabs { display: flex; gap: 5px; padding-bottom: 12px; overflow-x: auto; }.proxy-mode-tabs button { min-width: 78px; display: flex; align-items: center; justify-content: center; gap: 7px; padding: 8px 10px; color: #9099aa; background: #181b23; border: 1px solid #303544; border-radius: 5px; font-size: 10px; cursor: pointer; }.proxy-mode-tabs button.active { color: #fff; background: #1d2940; border-color: #3a67a7; }.proxy-mode-tabs i { width: 6px; height: 6px; background: #697285; border-radius: 50%; }.proxy-mode-tabs i.enabled { background: #30c991; box-shadow: 0 0 7px rgba(48,201,145,.65); }
.proxy-editor { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }.proxy-editor > label:not(.switch-control) { display: grid; gap: 6px; color: #aab2c1; font-size: 10px; }.proxy-editor > label > span { display: flex; justify-content: space-between; }.proxy-editor small { color: #697285; }.proxy-save-line { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.admin-table-wrap { overflow: auto; max-height: 480px; }.admin-table-wrap.compact { max-height: 325px; }table { width: 100%; min-width: 780px; border-collapse: collapse; }th, td { padding: 10px 9px; text-align: left; border-bottom: 1px solid #272b36; white-space: nowrap; }th { position: sticky; top: 0; color: #697285; background: #1b1e27; font-size: 9px; font-weight: 700; }td { color: #b7bfcd; font-size: 10px; }td code { color: #8fa0b8; }.compact table { min-width: 460px; }.status { display: inline-flex; padding: 4px 7px; color: #9099aa; background: #252a36; border-radius: 4px; }.status.active,.status.success { color: #5bd7ac; background: rgba(48,201,145,.1); }.status.running { color: #73aaff; background: rgba(52,136,255,.1); }.status.queued { color: #f3ad4d; background: rgba(243,173,77,.1); }.status.failed,.status.canceled,.status.disabled { color: #ef7a87; background: rgba(239,101,116,.1); }.table-empty { height: 100px; color: #697285; text-align: center; }
@media (max-width: 900px) { .admin-layout { grid-template-columns: 1fr; }.admin-sidebar { position: static; height: auto; display: flex; align-items: center; gap: 10px; padding: 10px 13px; overflow-x: auto; border-right: 0; border-bottom: 1px solid #272b36; }.admin-sidebar-title,.admin-sidebar-note { display: none; }.admin-sidebar nav { display: flex; min-width: max-content; }.admin-sidebar nav a { padding: 8px 10px; border-left: 0; border-bottom: 2px solid transparent; }.admin-sidebar nav a.active { border-bottom-color: #3488ff; }.admin-metrics { grid-template-columns: repeat(2,1fr); }.admin-grid,.cards-management,.infra-grid,.proxy-editor { grid-template-columns: 1fr; }.cards-management > .card-inventory-panel { grid-column: auto; }.settings-grid { grid-template-columns: 1fr; } }
@media (max-width: 620px) { .admin-topbar { padding: 0 12px; }.admin-content { padding: 20px 13px 40px; }.admin-heading { display: grid; }.admin-heading .button { width: 100%; }.admin-online { display: none; }.admin-brand strong { font-size: 12px; }.admin-top-actions .button { width: 34px; padding: 0; font-size: 0; }.admin-metrics { grid-template-columns: 1fr; }.admin-login-panel { padding: 24px; } }
@media (max-width: 620px) { .card-issue-panel .action-button, .card-fusion-panel .action-button { width: 100%; }.fusion-records-head { flex-wrap: wrap; }.fusion-records-head > div { width: 100%; }.fusion-records-head .search-field { flex-basis: calc(100% - 40px); }.generated-codes-head { align-items: stretch; flex-direction: column; }.generated-codes-head .button { width: 100%; } }

.admin-layout, .admin-content, .admin-grid, .admin-panel, .admin-panel-head, .admin-metrics article, .infra-grid article { min-width: 0; }
.admin-panel, .admin-metrics article, .infra-grid article { transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease; }
.admin-panel:hover, .admin-metrics article:hover, .infra-grid article:hover { border-color: #35496e; box-shadow: 0 10px 24px rgba(0,0,0,.14); }
.admin-sidebar nav a { transition: color .16s ease, background .16s ease, border-color .16s ease, transform .16s ease; }
.admin-sidebar nav a:hover { transform: translateX(2px); }
.admin-panel-head h2, .admin-panel-head p, .panel-copy, .admin-banner { overflow-wrap: anywhere; }
.admin-panel-head p { margin: 5px 0 0; color: #9099aa; font-size: 10px; line-height: 1.5; }
.data-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 13px; padding: 9px; background: #171a22; border: 1px solid #272b36; border-radius: 6px; }
.search-field, .select-field { display: flex; align-items: center; gap: 7px; min-width: 180px; flex: 1 1 220px; color: #697285; }
.select-field { flex: 0 1 165px; }
.search-field input, .select-field select { min-width: 0; width: 100%; margin: 0; }
.select-field select { color: #b7bfcd; }
.cell-subtitle { display: block; margin-top: 3px; color: #697285; font-size: 9px; white-space: normal; }
.remain-cell { color: #7bdbba; font-weight: 700; }
.user-cell { display: flex; align-items: center; gap: 8px; min-width: 180px; }
.user-cell > span:last-child { display: grid; gap: 3px; }
.user-cell small { color: #697285; font-size: 9px; }
.user-avatar { width: 28px; height: 28px; display: grid; place-items: center; color: #8ab7ff; background: #1d2940; border: 1px solid #365a91; border-radius: 50%; }
.online-dot { display: block; margin-top: 4px; color: #30c991; font-size: 9px; }
.pager { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 13px; color: #697285; font-size: 10px; }
.pager > div { display: flex; gap: 6px; }
.pager .icon-button { width: 30px; height: 30px; }
.policy-panel { max-width: 920px; }
.switch-control { display: flex; align-items: center; gap: 11px; min-height: 54px; margin: 10px 0; padding: 10px 12px; color: #b7bfcd; background: #171a22; border: 1px solid #272b36; border-radius: 8px; cursor: pointer; transition: border-color .18s ease, background .18s ease, transform .18s ease; }
.switch-control:hover { border-color: #3b5d8f; background: #1a202d; transform: translateY(-1px); }
.switch-control input { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
.switch-ui { position: relative; width: 38px; height: 22px; flex: none; background: #303544; border: 1px solid #465066; border-radius: 999px; transition: background .2s ease, border-color .2s ease, box-shadow .2s ease; }
.switch-ui::after { content: ""; position: absolute; top: 3px; left: 3px; width: 14px; height: 14px; background: #9aa4b5; border-radius: 50%; box-shadow: 0 1px 4px rgba(0,0,0,.35); transition: transform .2s cubic-bezier(.2,.8,.2,1), background .2s ease; }
.switch-control input:checked + .switch-ui { background: #2268c8; border-color: #4d9aff; box-shadow: 0 0 0 3px rgba(52,136,255,.12); }
.switch-control input:checked + .switch-ui::after { background: #fff; transform: translateX(16px); }
.switch-copy { display: grid !important; flex: 1; gap: 3px; }
.switch-copy b { color: #f1f3f7; font-size: 11px; }
.switch-copy small { color: #697285; font-size: 9px; line-height: 1.45; }
.switch-control > svg { color: #30c991; flex: none; }
.action-button { width: auto; min-width: 148px; margin-top: 0; align-self: flex-start; }
.card-issue-panel .action-button, .card-fusion-panel .action-button { min-height: 38px; border-radius: 9px; }
.generated-codes { display: grid; gap: 8px; margin-top: 14px; padding: 12px; background: rgba(48,201,145,.06); border: 1px solid rgba(48,201,145,.22); border-radius: 8px; }
.generated-codes-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.generated-codes-head > span { display: grid; gap: 3px; }
.generated-codes-head strong { color: #b7efdc; font-size: 11px; }
.generated-codes-head small { color: #697285; font-size: 9px; }
.generated-codes-head .button { min-height: 29px; padding: 0 9px; font-size: 10px; }
.generated-code-row, .code-with-action { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; padding: 8px 9px; background: #151a20; border: 1px solid rgba(48,201,145,.16); border-radius: 6px; }
.generated-code-row code, .code-with-action code { min-width: 0; overflow: hidden; color: #d3f9ec; font: 700 12px Consolas, monospace; text-overflow: ellipsis; white-space: nowrap; }
.copy-button { display: inline-flex; align-items: center; justify-content: center; gap: 4px; flex: none; min-height: 26px; padding: 0 8px; color: #8ab7ff; background: #1d2940; border: 1px solid #385884; border-radius: 5px; font-size: 9px; cursor: pointer; transition: transform .16s ease, background .16s ease, border-color .16s ease; }
.copy-button:hover { background: #263b60; border-color: #5799ff; transform: translateY(-1px); }
.fusion-records-head { display: flex; align-items: center; gap: 8px; margin: 14px 0 9px; }
.fusion-records-head > div { display: grid; gap: 3px; margin-right: auto; }
.fusion-records-head strong { color: #f1f3f7; font-size: 11px; }
.fusion-records-head small { color: #697285; font-size: 9px; }
.fusion-records-head .search-field { min-width: 180px; flex: 0 1 260px; }
.source-codes { display: grid; gap: 4px; min-width: 180px; }
.source-codes span { display: block; overflow: hidden; text-overflow: ellipsis; }
.source-codes code { color: #8fa0b8; font-size: 9px; }
.source-codes .copy-button { justify-self: start; }
.fusion-records-table { max-height: 360px; }
.policy-settings { margin-top: 16px; }
.policy-settings small { color: #697285; font-size: 9px; font-weight: 400; }
.admin-modal-backdrop { z-index: 50; }
.admin-user-modal { width: min(600px, calc(100vw - 26px)); }
.user-edit-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.user-edit-grid label, .edit-notes { display: grid; gap: 6px; color: #aab2c1; font-size: 10px; }
.edit-notes { margin-top: 12px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 15px; }
.mini-action { display: inline-flex; align-items: center; gap: 4px; }
.admin-banner { animation: admin-banner-in .2s ease both; }
.admin-table-wrap { width: 100%; overscroll-behavior-x: contain; scrollbar-color: #3a4355 transparent; }
.admin-table-wrap table { table-layout: auto; }
.proxy-save-line { flex-wrap: wrap; }
.proxy-save-line .panel-copy { min-width: 0; flex: 1 1 240px; }
.proxy-save-line .button { flex: 0 0 auto; }
.admin-top-actions { min-width: 0; }
.admin-brand { min-width: 0; }
.admin-brand strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@keyframes admin-banner-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }

@media (max-width: 900px) {
  .admin-content { overflow: hidden; }
  .admin-panel-head { flex-wrap: wrap; }
  .admin-panel-head > svg, .admin-panel-head > code, .admin-panel-head > b { margin-left: auto; }
  .infra-grid article { grid-template-columns: 34px minmax(0,1fr) auto; }
  .proxy-save-line { align-items: stretch; }
  .proxy-save-line .button { width: 100%; }
}

@media (max-width: 480px) {
  .admin-topbar { height: 56px; }
  .admin-layout { min-width: 0; }
  .admin-sidebar { top: 56px; }
  .admin-content { padding-left: 10px; padding-right: 10px; }
  .admin-panel { padding: 14px; }
  .admin-heading h1 { font-size: 22px; }
  .admin-table-wrap { margin-left: -2px; margin-right: -2px; }
}
</style>
