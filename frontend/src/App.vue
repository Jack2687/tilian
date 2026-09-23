<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  Activity, AlertCircle, Check, ChevronLeft, CircleDollarSign, Clock3, Copy,
  Database, Gauge, KeyRound, LayoutDashboard, Link2, LoaderCircle, Menu,
  Moon, Play, RefreshCw, ServerCog, ShieldCheck, Square, Trash2, Upload, Megaphone,
  FileUp, MessageCircle, UsersRound, WalletCards, X, TestTube2, ReceiptText, QrCode, ExternalLink,
} from "lucide-vue-next";
import { api, ApiError, getAuthToken, setAuthToken } from "./api/client";
import type { Channel, MetaResponse, StatsResponse, Task, User } from "./types";
import AdminView from "./views/AdminView.vue";
import ApiDocsView from "./views/ApiDocsView.vue";
import AuthView from "./views/AuthView.vue";

type ImportedAccount = { at: string; sessionToken?: string; email: string; atMask: string; status: string; type?: string; alg?: string; aud?: string; subject?: string; expired?: boolean; exp?: number };
type ChannelDraft = {
  importText: string;
  accounts: ImportedAccount[];
  concurrency: number;
  proxyMode: "builtin" | "custom";
  proxiesText: string;
  exitProxiesText: string;
  paramsText: string;
  country: string;
  currency: string;
  forceMethod: boolean;
  forceZero: boolean;
};
type WorkspaceDraft = {
  selectedMode?: string;
  importText?: string;
  accounts?: ImportedAccount[];
  concurrency?: number;
  proxyMode?: "builtin" | "custom";
  proxiesText?: string;
  exitProxiesText?: string;
  paramsText?: string;
  country?: string;
  currency?: string;
  forceMethod?: boolean;
  forceZero?: boolean;
  channels?: Record<string, Partial<ChannelDraft>>;
};

const WORKSPACE_DRAFT_KEY = "tilian:workspace-draft:v1";
function readWorkspaceDraft(): WorkspaceDraft {
  try {
    const value = JSON.parse(sessionStorage.getItem(WORKSPACE_DRAFT_KEY) || "null");
    return value && typeof value === "object" ? value as WorkspaceDraft : {};
  } catch {
    return {};
  }
}
const workspaceDraft = readWorkspaceDraft();

const channelDefaults: Record<string, { country: string; currency: string }> = {
  paypal: { country: "US", currency: "USD" }, gopay: { country: "ID", currency: "IDR" },
  gcash: { country: "PH", currency: "PHP" }, momo: { country: "VN", currency: "VND" },
  upi: { country: "IN", currency: "INR" }, blik: { country: "PL", currency: "PLN" },
  pix: { country: "BR", currency: "BRL" }, ideal: { country: "NL", currency: "EUR" },
  card: { country: "PH", currency: "PHP" },
};

const fallbackModes: Channel[] = [
  { id: "paypal", name: "PayPal", description: "协议提链" },
  { id: "gopay", name: "GoPay", description: "印尼钱包" },
  { id: "gcash", name: "GCash", description: "菲律宾钱包" },
  { id: "momo", name: "MoMo", description: "越南钱包" },
  { id: "upi", name: "UPI", description: "印度支付" },
  { id: "blik", name: "BLIK", description: "波兰支付" },
  { id: "pix", name: "PIX", description: "巴西支付" },
  { id: "ideal", name: "iDEAL", description: "荷兰支付" },
  { id: "card", name: "直卡", description: "卡片流程" },
];
const isAdminView = window.location.pathname === "/admin" || window.location.pathname === "/admin.html";
const isApiDocsView = window.location.pathname === "/api-doc.html" || window.location.pathname === "/api-docs";
const authReady = ref(false);
const currentUser = ref<User | null>(null);
const selectedMode = ref(workspaceDraft.selectedMode || "paypal");

function defaultChannelDraft(mode: string, source: Partial<ChannelDraft> = {}): ChannelDraft {
  const defaults = channelDefaults[mode] || channelDefaults.paypal;
  return {
    importText: source.importText || "",
    accounts: Array.isArray(source.accounts) ? source.accounts.slice(0, 100) : [],
    concurrency: Math.max(1, Number(source.concurrency) || 3),
    proxyMode: source.proxyMode === "custom" ? "custom" : "builtin",
    proxiesText: source.proxiesText || "",
    exitProxiesText: source.exitProxiesText || "",
    paramsText: source.paramsText || "",
    country: source.country || defaults.country,
    currency: source.currency || defaults.currency,
    forceMethod: source.forceMethod === true,
    forceZero: source.forceZero === true,
  };
}

const initialChannelDraft = defaultChannelDraft(
  selectedMode.value,
  workspaceDraft.channels?.[selectedMode.value] || (workspaceDraft.channels ? {} : {
    ...workspaceDraft,
    forceMethod: undefined,
    forceZero: undefined,
  }),
);
const channelDrafts = ref<Record<string, ChannelDraft>>({ [selectedMode.value]: initialChannelDraft });

const sidebarOpen = ref(true);
const mobileNavOpen = ref(false);
const meta = ref<MetaResponse | null>(null);
const stats = ref<StatsResponse>({ totalTasks: 0, totalLinkSuccess: 0, totalFailed: 0, totalCallbackPaid: 0, successRate: 0 });
const cardCode = ref(sessionStorage.getItem("tilian:cdk") || "");
const cardState = ref<{ remain: number; status: string; maxConcurrency?: number } | null>(null);
const importText = ref(initialChannelDraft.importText);
const accounts = ref<ImportedAccount[]>(initialChannelDraft.accounts.slice());
const tasks = ref<Task[]>([]);
const heatTimeline = ref<import("./types").SuccessTimeline | null>(null);
const heatScheme = ref("all");
const fileInput = ref<HTMLInputElement | null>(null);
const concurrency = ref(initialChannelDraft.concurrency);
const proxyMode = ref<"builtin" | "custom">(initialChannelDraft.proxyMode);
const proxiesText = ref(initialChannelDraft.proxiesText);
const exitProxiesText = ref(initialChannelDraft.exitProxiesText);
const paramsText = ref(initialChannelDraft.paramsText);
const country = ref(initialChannelDraft.country);
const currency = ref(initialChannelDraft.currency);
const forceMethod = ref(initialChannelDraft.forceMethod);
const forceZero = ref(initialChannelDraft.forceZero);
const fuseOpen = ref(false);
const fuseCodesText = ref("");
const fuseRemark = ref("CDK 融合");
const fuseSubmitting = ref(false);
const fuseConfirmed = ref(false);
const fusedCard = ref<{ code: string; quota: number; remain: number } | null>(null);
const checkingCard = ref(false);
const importing = ref(false);
const submitting = ref(false);
const loadingTasks = ref(false);
const paymentLoading = ref(false);
const plusLoading = ref(false);
const eventsLoading = ref(false);
const utilityError = ref("");
const toast = ref<{ type: "ok" | "error"; message: string } | null>(null);
const announcement = ref<{ title: string; content: string } | null>(null);
const blikCodes = ref<Record<string, string>>({});
const proxyChecks = ref<Array<{ proxy: string; ok: boolean; ip?: string; latencyMs?: number; error?: string }>>([]);
const proxyChecking = ref(false);
const paymentOpen = ref(false);
const paymentEnabled = ref(false);
const paymentPlans = ref<Array<{ id: string; label: string; quota: number; money: number }>>([]);
const plusOpen = ref(false);
const plusRecords = ref<Array<{ email: string; t: string; taskId: string }>>([]);
const eventsOpen = ref<string | null>(null);
const taskEvents = ref<Array<{ eventId: string; type: string; itemIndex?: number; payload?: Record<string, unknown>; createdAt: string }>>([]);
const expandedTaskItems = ref<Record<string, boolean>>({});
const collapsedTasks = ref<Record<string, boolean>>({});
const observedTaskStatuses = ref<Record<string, string>>({});
const successNotifiedTasks = ref<Record<string, boolean>>({});
const qrOpen = ref(false);
const qrSrc = ref("");
const qrTitle = ref("提链二维码");
let pollTimer: number | null = null;
let toastTimer: number | null = null;
let autoParseTimer: number | null = null;
let lastParsedImport = "";

const modes = computed(() => meta.value?.modes?.length ? meta.value.modes : fallbackModes);
const currentMode = computed(() => modes.value.find((mode) => mode.id === selectedMode.value) || modes.value[0]);
function taskPriority(task: Task) {
  if (["success", "plus_success"].includes(task.status)) return 3;
  if (["queued", "running", "waiting_blik", "waiting_plus"].includes(task.status)) return 2;
  return 1;
}

function taskUpdatedAt(task: Task) {
  const values = [task.updatedAt, task.finishedAt, task.createdAt,
    ...(task.items || []).flatMap((item) => [item.finishedAt, item.startedAt])];
  return values.reduce<number>((latest, value) => {
    if (value == null) return latest;
    const numeric = Number(value);
    const timestamp = Number.isFinite(numeric) && numeric > 0 ? numeric : Date.parse(String(value));
    return Number.isFinite(timestamp) ? Math.max(latest, timestamp) : latest;
  }, 0);
}

const visibleTasks = computed(() => tasks.value
  .filter((task) => task.mode === selectedMode.value)
  .slice()
  .sort((left, right) => taskPriority(right) - taskPriority(left) || taskUpdatedAt(right) - taskUpdatedAt(left) || right.id.localeCompare(left.id)));
const activeTask = computed(() => visibleTasks.value.find((task) => ["queued", "running", "waiting_blik", "waiting_plus"].includes(task.status)) || visibleTasks.value[0]);
const aggregate = computed(() => {
  const items = visibleTasks.value.flatMap((task) => task.items || []);
  return {
    total: items.length,
    running: items.filter((item) => ["running", "waiting_blik", "waiting_plus"].includes(item.status)).length,
    success: items.filter((item) => ["success", "plus_success"].includes(item.status)).length,
    failed: items.filter((item) => ["failed", "timed_out"].includes(item.status)).length,
  };
});
const canSubmit = computed(() => Boolean(cardState.value?.status === "active" && accounts.value.length && !submitting.value));
const submitHint = computed(() => {
  if (submitting.value) return "正在提交任务…";
  if (!cardCode.value.trim()) return "请先输入并校验 CDK";
  if (!cardState.value) return "请先点击顶部“CDK 校验”";
  if (cardState.value.status !== "active") return "当前 CDK 不可用";
  if (!accounts.value.length) return "请先导入至少一个账号";
  return `将提交 ${accounts.value.length} 个账号`;
});
const heatCells = computed(() => heatTimeline.value?.cells || []);
const heatTitle = computed(() => heatScheme.value === "all" ? "全部通道" : (heatTimeline.value?.modes[heatScheme.value] || heatScheme.value));
const currentProxyPool = computed(() => meta.value?.engine.proxyPools?.[selectedMode.value]);
const concurrencyMax = computed(() => {
  const configured = meta.value;
  if (!configured) return 10;
  const limits = configured?.limits;
  const candidates = [
    limits?.cdkConcurrency,
    cardState.value?.maxConcurrency,
    limits?.userConcurrency,
    configured?.user?.taskConcurrency,
    currentUser.value?.taskConcurrency,
    limits?.maxConcurrency,
    configured?.engine.maxConcurrency,
  ]
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value) && value > 0);
  return Math.max(1, Math.floor(candidates.length ? Math.min(...candidates) : 1));
});
const heatTotal = computed(() => heatScheme.value === "all" ? heatTimeline.value?.totals.all || 0 : heatTimeline.value?.totals.byMode[heatScheme.value] || 0);

function toggleNavigation() {
  if (window.matchMedia("(max-width: 760px)").matches) mobileNavOpen.value = !mobileNavOpen.value;
  else sidebarOpen.value = !sidebarOpen.value;
}

function notify(message: string, type: "ok" | "error" = "ok") {
  toast.value = { type, message };
  if (toastTimer) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { toast.value = null; }, 3600);
}

function statusLabel(status: string) {
  return ({ queued: "排队中", running: "执行中", waiting_blik: "等待动态码", waiting_plus: "等待到账", success: "成功", plus_success: "已到账", failed: "失败", timed_out: "超时", canceled: "已取消" } as Record<string, string>)[status] || status;
}

const stageNames: Record<string, string> = {
  start: "开始执行",
  starting: "开始执行",
  retry: "自动重试",
  proxy: "代理准备",
  auth: "校验登录会话",
  promo: "检查优惠资格",
  warmup: "预热支付页面",
  sentinel: "生成风控上下文",
  checkout: "创建 Checkout",
  state: "准备支付状态",
  init: "初始化支付页面",
  elements: "初始化支付组件",
  taxes: "校验税费账单",
  amount: "校验 0 元金额",
  billing: "保存账单信息",
  token: "生成确认令牌",
  confirm: "确认 UPI 支付",
  approve: "订单风控审批",
  poll: "轮询支付状态",
  redirect: "解析支付跳转",
  gateway: "生成支付链接",
  worker_claimed: "Worker 接单",
  proxy_prepare: "准备代理",
  proxy_check: "检测代理",
  create_payment: "创建支付",
  payment_page: "读取支付页面",
  submit_payment: "提交支付",
  verify_payment: "验证支付结果",
  waiting_blik: "等待 BLIK 动态码",
  blik_submitted: "提交 BLIK 动态码",
  waiting_plus: "等待到账确认",
  callback: "等待回调",
  engine_execute: "执行提链引擎",
  engine_timeout: "引擎执行超时",
  engine_no_result: "引擎未返回结果",
  engine_exception: "引擎执行异常",
  completed: "执行完成",
};

function stageLabel(stage?: string | null) {
  const value = String(stage || "").trim();
  return value ? (stageNames[value] || value) : "等待执行";
}

function taskItemKey(taskId: string, itemIndex: number) {
  return `${taskId}:${itemIndex}`;
}

function isFailedTaskItem(item: Task["items"][number]) {
  return ["failed", "timed_out"].includes(item.status) || Boolean(item.error || item.errorCode);
}

function itemStatusHint(item: Task["items"][number]) {
  if (!isFailedTaskItem(item) && !["canceled", "timed_out"].includes(item.status)) return "";
  const text = String(item.error || item.stageDetail || "").toLowerCase();
  if (/promo|优惠|无资格|ineligible|not eligible|already redeemed|redeemed/.test(text)) {
    return "当前账号未满足该通道优惠资格，任务已停止重试。";
  }
  if (/payment method|支付方式|does not offer upi|method unavailable|通道不可用/.test(text)) {
    return "当前账号或环境未提供此支付方式，任务已终止。";
  }
  if (["canceled", "timed_out"].includes(item.status)) {
    return item.status === "canceled" ? "任务已被用户终止。" : "任务执行超时，已终止本次尝试。";
  }
  return "";
}

function taskStatusHint(task: Task) {
  if (task.status === "canceled") return "任务已终止，未完成的账号不会继续执行。";
  if (task.status === "timed_out") return "任务执行超时，未完成的账号已停止。";
  return "";
}

function itemDetailsOpen(taskId: string, item: Task["items"][number]) {
  return isFailedTaskItem(item) || expandedTaskItems.value[taskItemKey(taskId, item.index)] === true;
}

function toggleItemDetails(taskId: string, itemIndex: number) {
  const key = taskItemKey(taskId, itemIndex);
  expandedTaskItems.value[key] = !expandedTaskItems.value[key];
}

function taskExpanded(taskId: string) {
  return collapsedTasks.value[taskId] !== true;
}

function toggleTaskExpanded(taskId: string) {
  collapsedTasks.value[taskId] = taskExpanded(taskId);
}

function taskSuccessCount(task: Task) {
  return Number(task.progress?.success || (task.items || []).filter((item) => ["success", "plus_success"].includes(item.status)).length || 0);
}

function taskIsSuccessful(task: Task) {
  return ["success", "plus_success"].includes(task.status);
}

function observeTaskStatuses(nextTasks: Task[]) {
  const currentIds = new Set(nextTasks.map((task) => task.id));
  for (const task of nextTasks) {
    const previous = observedTaskStatuses.value[task.id];
    const successful = taskIsSuccessful(task) && taskSuccessCount(task) > 0;
    if (successful && previous && !["success", "plus_success"].includes(previous) && !successNotifiedTasks.value[task.id]) {
      notify(`${task.modeName || task.mode} 提链成功`);
      successNotifiedTasks.value[task.id] = true;
    } else if (successful && !previous) {
      // Existing successful tasks loaded on first refresh must not replay a toast.
      successNotifiedTasks.value[task.id] = true;
    }
    observedTaskStatuses.value[task.id] = task.status;
  }
  for (const taskId of Object.keys(collapsedTasks.value)) if (!currentIds.has(taskId)) delete collapsedTasks.value[taskId];
  for (const taskId of Object.keys(observedTaskStatuses.value)) if (!currentIds.has(taskId)) delete observedTaskStatuses.value[taskId];
  for (const taskId of Object.keys(successNotifiedTasks.value)) if (!currentIds.has(taskId)) delete successNotifiedTasks.value[taskId];
}

function eventTypeLabel(type: string) {
  return ({
    "task.created.v1": "任务已创建",
    "task.item.started.v1": "账号开始执行",
    "task.item.completed.v1": "账号执行结束",
  } as Record<string, string>)[type] || type;
}

function maskToken(value: string) {
  const trimmed = value.trim();
  return trimmed.length <= 20 ? trimmed : `${trimmed.slice(0, 12)}...${trimmed.slice(-6)}`;
}

function tokenEmail(value: string) {
  try {
    const body = value.split(".")[1];
    if (!body) return "待识别账号";
    const decoded = JSON.parse(decodeURIComponent(escape(atob(body.replace(/-/g, "+").replace(/_/g, "/")))));
    return decoded.email || decoded.preferred_username || decoded.sub || "待识别账号";
  } catch {
    return "待识别账号";
  }
}

function expiryLabel(exp?: number) {
  if (!exp) return "未提供";
  return new Date(exp * 1000).toLocaleString();
}

function normalizeConcurrency() {
  const max = concurrencyMax.value;
  const next = Math.min(max, Math.max(1, Math.floor(Number(concurrency.value) || 1)));
  if (concurrency.value !== next) concurrency.value = next;
}

async function loadOverview() {
  const [metaResult, statsResult, heatResult, announcementResult] = await Promise.allSettled([api.meta(), api.stats(), api.recentSuccess(), api.announcement()]);
  if (metaResult.status === "fulfilled") {
    meta.value = metaResult.value;
    if (workspaceDraft.concurrency == null) {
      concurrency.value = Math.min(metaResult.value.engine.defaultConcurrency || 3, concurrencyMax.value);
    }
    normalizeConcurrency();
  }
  if (statsResult.status === "fulfilled") stats.value = statsResult.value;
  if (heatResult.status === "fulfilled") heatTimeline.value = heatResult.value;
  if (announcementResult.status === "fulfilled") announcement.value = announcementResult.value.announcement;
}

function fillSample() {
  importText.value = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.sample-token-value";
}

function importFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  void file.text().then((text) => { importText.value = text; notify(`已读取 ${file.name}，请确认后导入`); });
  (event.target as HTMLInputElement).value = "";
}

function heatCount(cell: { total: number; byMode: Record<string, number> }) {
  return heatScheme.value === "all" ? cell.total : (cell.byMode[heatScheme.value] || 0);
}

function heatTooltip(cell: { start: number; end: number; total: number; byMode: Record<string, number> }) {
  return `${new Date(cell.start).toLocaleString()}\n${heatTitle.value}: ${heatCount(cell)} 次`;
}

async function clearAllTasks() {
  if (!cardCode.value.trim()) return notify("请先校验 CDK", "error");
  try { await api.clearTasks(cardCode.value.trim()); tasks.value = []; observeTaskStatuses([]); notify("当前 CDK 的任务已清理"); } catch (error) { notify(error instanceof Error ? error.message : "清理失败", "error"); }
}

async function testProxies() {
  const values = (proxyMode.value === "custom" ? proxiesText.value : "").split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  if (!values.length) return notify("请先选择自定义代理并填写代理池", "error");
  proxyChecking.value = true;
  proxyChecks.value = [];
  for (const proxy of values.slice(0, 20)) {
    try { proxyChecks.value.push(await api.checkProxy(proxy)); } catch (error) { proxyChecks.value.push({ proxy, ok: false, error: error instanceof Error ? error.message : "检测失败" }); }
  }
  proxyChecking.value = false;
  notify(`已完成 ${proxyChecks.value.length} 条代理检测`);
}

async function openPayment() {
  if (paymentLoading.value) return;
  paymentOpen.value = true;
  paymentLoading.value = true;
  utilityError.value = "";
  try { const result = await api.paymentConfig(); paymentEnabled.value = result.enabled; paymentPlans.value = result.plans || []; }
  catch (error) { utilityError.value = error instanceof Error ? error.message : "支付配置读取失败"; }
  finally { paymentLoading.value = false; }
}

async function openPlusRecords() {
  if (plusLoading.value) return;
  plusOpen.value = true;
  plusLoading.value = true;
  utilityError.value = "";
  try { plusRecords.value = (await api.plusRecords()).records || []; }
  catch (error) { utilityError.value = error instanceof Error ? error.message : "到账记录读取失败"; }
  finally { plusLoading.value = false; }
}

async function openEvents(task: Task) {
  if (eventsLoading.value) return;
  eventsOpen.value = task.id;
  eventsLoading.value = true;
  taskEvents.value = [];
  utilityError.value = "";
  try { taskEvents.value = await api.taskEvents(task.id, cardCode.value.trim()); }
  catch (error) { utilityError.value = error instanceof Error ? error.message : "事件读取失败"; }
  finally { eventsLoading.value = false; }
}

function openQr(item: Task["items"][number]) {
  const extra = item.extra || {};
  const value = String(item.qrImage || extra.qrImage || extra.qr_image || "");
  if (!value) return notify("当前任务还没有返回二维码图片", "error");
  qrSrc.value = value;
  qrTitle.value = `${item.email || "账号"} · 提链二维码`;
  qrOpen.value = true;
}

async function checkCard() {
  if (checkingCard.value) return;
  if (!cardCode.value.trim()) return notify("请输入 CDK", "error");
  checkingCard.value = true;
  try {
    const result = await api.checkCard(cardCode.value.trim());
    cardState.value = { remain: result.remain, status: result.status, maxConcurrency: result.maxConcurrency || result.cdkConcurrency };
    normalizeConcurrency();
    sessionStorage.setItem("tilian:cdk", cardCode.value.trim().toUpperCase());
    notify(`CDK 可用，剩余 ${result.remain} 次`);
    await refreshTasks();
  } catch (error) {
    cardState.value = null;
    notify(error instanceof Error ? error.message : "CDK 查询失败", "error");
  } finally {
    checkingCard.value = false;
  }
}

async function submitFuse() {
  if (fuseSubmitting.value) return;
  if (!fuseConfirmed.value) return notify("请先确认来源 CDK 将永久失效", "error");
  const codes = fuseCodesText.value.split(/[\s,，;；]+/).map((item) => item.trim().toUpperCase()).filter(Boolean);
  if (codes.length < 2) return notify("至少输入 2 个不同的 CDK", "error");
  if (new Set(codes).size !== codes.length) return notify("融合列表中存在重复 CDK", "error");
  fuseSubmitting.value = true;
  try {
    const result = await api.fuseCards(codes, fuseRemark.value);
    fusedCard.value = result.card;
    fuseCodesText.value = "";
    fuseConfirmed.value = false;
    if (codes.includes(cardCode.value.trim().toUpperCase())) { cardCode.value = ""; cardState.value = null; sessionStorage.removeItem("tilian:cdk"); }
    notify("CDK 融合成功，请立即保存新卡");
  } catch (error) { notify(error instanceof Error ? error.message : "CDK 融合失败", "error"); }
  finally { fuseSubmitting.value = false; }
}

async function copyFusedCard() {
  if (!fusedCard.value) return;
  await copyLink(fusedCard.value.code);
}

async function parseImportedAccounts(source: string, showError = true) {
  const text = source.trim();
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (!lines.length) {
    if (showError) notify("请先粘贴 AT，每行一条", "error");
    return false;
  }
  if (text === lastParsedImport) return true;
  importing.value = true;
  try {
    const parsed = await api.parseAccounts(text);
    accounts.value = parsed.items.map((item) => ({
      at: item.at || "",
      sessionToken: item.sessionToken || "",
      email: item.email || tokenEmail(item.at || ""),
      atMask: item.atMask || maskToken(item.at || ""),
      status: "ready",
      type: item.type,
      alg: item.alg,
      aud: item.aud,
      subject: item.subject,
      expired: item.expired,
      exp: item.exp,
    }));
    lastParsedImport = text;
    if (showError) notify(`已解析 ${accounts.value.length} 个账号`);
    return true;
  } catch (error) {
    if (showError) notify(error instanceof Error ? error.message : "账号解析失败", "error");
    return false;
  } finally {
    importing.value = false;
  }
}

async function importAccounts() {
  await parseImportedAccounts(importText.value, true);
}

function scheduleImportParsing(value: string) {
  if (autoParseTimer) window.clearTimeout(autoParseTimer);
  if (!value.trim()) {
    lastParsedImport = "";
    return;
  }
  if (value.trim() === lastParsedImport) return;
  autoParseTimer = window.setTimeout(() => {
    autoParseTimer = null;
    void parseImportedAccounts(value, false);
  }, 320);
}

function removeAccount(index: number) {
  accounts.value.splice(index, 1);
}

async function createTask() {
  if (!canSubmit.value) return;
  if (!cardState.value) return notify("请先校验 CDK", "error");
  if (accounts.value.some((account) => account.expired)) return notify("账号列表包含已过期 AT，请删除后重试", "error");
  if (proxyMode.value === "custom" && !proxiesText.value.trim()) return notify("自定义代理模式需要填写代理池", "error");
  submitting.value = true;
  try {
    let advancedParams: Record<string, unknown> = {};
    if (paramsText.value.trim()) {
      try { advancedParams = JSON.parse(paramsText.value); } catch { return notify("附加参数不是有效 JSON", "error"); }
    }
    const requestId = crypto.randomUUID();
    const task = await api.createTask({
      mode: selectedMode.value,
      cardCode: cardCode.value.trim(),
      accounts: accounts.value.map((account) => ({ email: account.email, at: account.at, sessionToken: account.sessionToken || "" })),
      concurrency: concurrency.value,
      proxyMode: proxyMode.value,
      proxiesText: proxiesText.value,
      exitProxiesText: exitProxiesText.value,
      paramsText: JSON.stringify({ ...advancedParams, country: country.value, currency: currency.value, forceMethod: forceMethod.value, forceZero: forceZero.value }),
    }, requestId);
    tasks.value = [task, ...tasks.value.filter((item) => item.id !== task.id)];
    observeTaskStatuses(tasks.value);
    notify(`任务 ${task.id} 已提交`);
    startPolling();
  } catch (error) {
    notify(error instanceof ApiError ? error.message : "任务提交失败", "error");
  } finally {
    submitting.value = false;
  }
}

async function refreshTasks() {
  if (!cardCode.value.trim() || loadingTasks.value) return;
  loadingTasks.value = true;
  try {
    tasks.value = await api.listTasks(cardCode.value.trim());
    observeTaskStatuses(tasks.value);
  } catch (error) {
    if (error instanceof ApiError && error.status !== 404) notify(error.message, "error");
  } finally {
    loadingTasks.value = false;
  }
}

async function cancelTask(task: Task) {
  try {
    const updated = await api.cancelTask(task.id, cardCode.value.trim());
    tasks.value = tasks.value.map((item) => item.id === updated.id ? updated : item);
    observeTaskStatuses(tasks.value);
    notify("任务已取消");
  } catch (error) {
    notify(error instanceof Error ? error.message : "取消失败", "error");
  }
}

async function submitBlik(task: Task, itemIndex: number) {
  const key = `${task.id}:${itemIndex}`;
  const code = (blikCodes.value[key] || "").trim();
  if (!/^\d{6}$/.test(code)) return notify("请输入 6 位 BLIK 动态码", "error");
  try { const updated = await api.submitBlikCode(task.id, cardCode.value.trim(), itemIndex, code); tasks.value = tasks.value.map((item) => item.id === updated.id ? updated : item); observeTaskStatuses(tasks.value); notify("BLIK 动态码已提交"); } catch (error) { notify(error instanceof Error ? error.message : "动态码提交失败", "error"); }
}

async function refreshQr(task: Task, itemIndex: number) {
  try { const updated = await api.refreshQr(task.id, cardCode.value.trim(), itemIndex); tasks.value = tasks.value.map((item) => item.id === updated.id ? updated : item); observeTaskStatuses(tasks.value); notify("已请求刷新二维码"); } catch (error) { notify(error instanceof Error ? error.message : "二维码刷新失败", "error"); }
}

function canRefreshQr(item: Task["items"][number]) {
  return ["running", "waiting_plus"].includes(item.status) && Boolean(item.extra?.qrRefreshActive || item.extra?.qr_refresh_active);
}

async function copyLink(link?: string | null) {
  if (!link) return;
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(link);
    notify("已复制到剪贴板");
  } catch {
    const fallback = document.createElement("textarea");
    fallback.value = link;
    fallback.setAttribute("readonly", "true");
    fallback.style.position = "fixed";
    fallback.style.opacity = "0";
    document.body.appendChild(fallback);
    fallback.select();
    const copied = document.execCommand("copy");
    fallback.remove();
    if (copied) notify("已复制到剪贴板");
    else if (typeof window.prompt === "function") window.prompt("复制链接（Ctrl+C）", link);
    else notify("复制失败，请手动复制链接", "error");
  }
}

function saveCurrentChannelDraft() {
  channelDrafts.value[selectedMode.value] = {
    importText: importText.value,
    accounts: accounts.value.slice(0, 100),
    concurrency: concurrency.value,
    proxyMode: proxyMode.value,
    proxiesText: proxiesText.value,
    exitProxiesText: exitProxiesText.value,
    paramsText: paramsText.value,
    country: country.value,
    currency: currency.value,
    forceMethod: forceMethod.value,
    forceZero: forceZero.value,
  };
}

function loadChannelDraft(id: string) {
  const draft = channelDrafts.value[id] || defaultChannelDraft(id);
  channelDrafts.value[id] = draft;
  importText.value = draft.importText;
  accounts.value = draft.accounts.slice();
  concurrency.value = draft.concurrency;
  proxyMode.value = draft.proxyMode;
  proxiesText.value = draft.proxiesText;
  exitProxiesText.value = draft.exitProxiesText;
  paramsText.value = draft.paramsText;
  country.value = draft.country;
  currency.value = draft.currency;
  forceMethod.value = draft.forceMethod;
  forceZero.value = draft.forceZero;
}

function selectMode(id: string) {
  if (id === selectedMode.value) return;
  saveCurrentChannelDraft();
  selectedMode.value = id;
  heatScheme.value = id;
  loadChannelDraft(id);
  proxyChecks.value = [];
  proxyChecking.value = false;
  blikCodes.value = {};
  expandedTaskItems.value = {};
  eventsOpen.value = null;
  mobileNavOpen.value = false;
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = window.setInterval(() => void refreshTasks(), 2500);
}

async function onAuthenticated(value: { token: string; user: User }) {
  setAuthToken(value.token);
  currentUser.value = value.user;
  authReady.value = true;
  await loadOverview();
  if (cardCode.value) await checkCard();
  startPolling();
}

function logoutUser() {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
  setAuthToken("");
  currentUser.value = null;
  cardState.value = null;
  tasks.value = [];
  observeTaskStatuses([]);
}

let draftTimer: number | null = null;
function persistWorkspaceDraft() {
  saveCurrentChannelDraft();
  if (draftTimer) window.clearTimeout(draftTimer);
  draftTimer = window.setTimeout(() => {
    sessionStorage.setItem(WORKSPACE_DRAFT_KEY, JSON.stringify({
      selectedMode: selectedMode.value,
      channels: channelDrafts.value,
    } satisfies WorkspaceDraft));
    draftTimer = null;
  }, 120);
}

watch(cardCode, (value) => {
  cardState.value = null;
  if (value.trim()) sessionStorage.setItem("tilian:cdk", value.trim().toUpperCase());
  else sessionStorage.removeItem("tilian:cdk");
  persistWorkspaceDraft();
});
watch(importText, scheduleImportParsing);
watch([selectedMode, importText, accounts, concurrency, proxyMode, proxiesText, exitProxiesText, paramsText, country, currency, forceMethod, forceZero], persistWorkspaceDraft, { deep: true });
watch([concurrencyMax, selectedMode], normalizeConcurrency);

onMounted(async () => {
  if (isAdminView || isApiDocsView) return;
  scheduleImportParsing(importText.value);
  const token = getAuthToken();
  if (token) {
    try { currentUser.value = (await api.me()).user; }
    catch { setAuthToken(""); }
  }
  authReady.value = true;
  if (currentUser.value) {
    await loadOverview();
    if (cardCode.value) await checkCard();
    startPolling();
  }
});

onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer);
  if (toastTimer) window.clearTimeout(toastTimer);
  if (draftTimer) window.clearTimeout(draftTimer);
  if (autoParseTimer) window.clearTimeout(autoParseTimer);
});
</script>

<template>
  <AdminView v-if="isAdminView" />
  <ApiDocsView v-else-if="isApiDocsView" />
  <AuthView v-else-if="!authReady || !currentUser" @authenticated="onAuthenticated" />
  <div v-else class="app-shell" :class="{ 'sidebar-collapsed': !sidebarOpen }">
    <header class="topbar">
      <div class="topbar-main">
      <button class="icon-button menu-button" type="button" title="展开或收起导航" aria-controls="channel-sidebar" :aria-expanded="mobileNavOpen || sidebarOpen" @click="toggleNavigation">
        <Menu :size="19" />
      </button>
      <div class="brand">
        <span class="brand-mark"><Link2 :size="20" /></span>
        <strong>DJB 提链控制台</strong>
        <span class="live-badge"><i /> LIVE</span>
      </div>
      <div class="topbar-spacer" />
      <div class="topbar-cdk">
        <KeyRound :size="14" />
        <input v-model="cardCode" type="password" aria-label="CDK" placeholder="输入 CDK" @keyup.enter="checkCard" />
        <button type="button" @click="checkCard" :disabled="checkingCard"><LoaderCircle v-if="checkingCard" :size="13" class="spin" />{{ checkingCard ? '校验中' : 'CDK 校验' }}</button>
        <span v-if="cardState" class="topbar-remain">余 {{ cardState.remain }}</span>
      </div>
      <div class="service-status" :class="{ offline: !meta }"><i />{{ meta ? 'API 已连接' : 'API 未连接' }}</div>
       <div class="service-status optional"><Database :size="14" /> {{ meta?.engine.ha?.enabled ? '集群模式' : '兼容模式' }}</div>
       <button class="user-chip" type="button" title="退出登录" @click="logoutUser"><span>{{ (currentUser.displayName || currentUser.username).slice(0, 1).toUpperCase() }}</span>{{ currentUser.displayName || currentUser.username }}</button>
      </div>
      <nav class="topbar-tools" aria-label="常用功能">
      <a class="topbar-link" title="换绑" href="https://tl.sayt.cloud/rebind/" target="_blank" rel="noopener noreferrer"><RefreshCw :size="14" />换绑</a>
      <a class="topbar-link" title="检测资格" href="https://tl.sayt.cloud/detect/" target="_blank" rel="noopener noreferrer"><ShieldCheck :size="14" />检测资格</a>
      <a class="topbar-link" title="加入群聊" href="https://qm.qq.com/q/sdLQgWmtLU" target="_blank" rel="noopener noreferrer"><MessageCircle :size="14" />加入群聊</a>
      <button class="topbar-link topbar-button" title="在线充值" type="button" @click="openPayment"><CircleDollarSign :size="14" />在线充值</button>
      <button class="topbar-link topbar-button" title="到账记录" type="button" @click="openPlusRecords"><ReceiptText :size="14" />到账记录</button>
      <button class="topbar-link topbar-button" title="CDK 融合" type="button" @click="fuseOpen = true"><WalletCards :size="14" />CDK 融合</button>
      <a class="topbar-link" title="API 对接" href="/api-doc.html"><Link2 :size="14" />API 对接</a>
      <a class="topbar-link" title="管理后台" href="/admin"><ServerCog :size="14" />管理</a>
      <button class="icon-button" type="button" title="深色主题"><Moon :size="18" /></button>
      </nav>
    </header>

    <aside id="channel-sidebar" class="sidebar" :class="{ 'mobile-open': mobileNavOpen }">
      <div class="sidebar-head">
        <span>提链流程</span>
        <button class="icon-button sidebar-close" type="button" title="关闭导航" @click="mobileNavOpen = false"><X :size="18" /></button>
      </div>
      <nav class="channel-nav">
        <button v-for="mode in modes" :key="mode.id" type="button" :class="{ active: selectedMode === mode.id }" @click="selectMode(mode.id)">
          <WalletCards :size="17" />
          <span><b>{{ mode.name }}</b><small>{{ mode.description || '支付提链' }}</small></span>
        </button>
      </nav>
      <div class="sidebar-section-label">快捷导航</div>
      <nav class="quick-nav">
        <a href="#overview"><LayoutDashboard :size="16" />状态看板</a>
        <a href="#setup"><KeyRound :size="16" />账号与 CDK</a>
        <a href="#tasks"><Activity :size="16" />任务记录</a>
      </nav>
      <div class="sidebar-foot">
        <ShieldCheck :size="17" />
        <span><b>安全执行</b><small>凭证不会写入浏览器日志</small></span>
      </div>
    </aside>

    <button v-if="mobileNavOpen" class="nav-backdrop" type="button" aria-label="关闭导航" @click="mobileNavOpen = false" />

    <main class="content">
      <section class="page-intro" id="overview">
        <div>
          <p class="eyebrow">MULTI CHANNEL / ASYNC WORKER / CLUSTER READY</p>
          <h1>{{ currentMode?.name }} 提链工作台</h1>
          <p>统一管理账号、额度和异步任务，每个账号的执行状态与结果保持在同一行。</p>
        </div>
        <div class="intro-actions">
          <button class="button secondary" type="button" :disabled="loadingTasks" @click="refreshTasks"><RefreshCw :size="16" :class="{ spin: loadingTasks }" />刷新</button>
          <span class="button secondary intro-status"><Activity :size="16" />{{ heatTitle }}成功 {{ heatTotal }}</span>
        </div>
      </section>

      <section v-if="announcement" class="announcement-bar" aria-live="polite"><Megaphone :size="17" /><div><strong>{{ announcement.title }}</strong><span>{{ announcement.content }}</span></div></section>

      <section class="heat-panel" aria-label="最近成功热力格">
        <div class="heat-head"><div><span class="section-kicker">SUCCESS HEATMAP · 24 HOURS</span><h2>{{ heatTitle }}最近成功</h2></div><select v-model="heatScheme"><option value="all">全部通道</option><option v-for="(name, id) in heatTimeline?.modes" :key="id" :value="id">{{ name }}</option></select></div>
        <div class="heat-grid"><span v-for="(cell, index) in heatCells" :key="index" class="heat-cell" :class="{ active: heatCount(cell) > 0, strong: heatCount(cell) > 2 }" :title="heatTooltip(cell)" /></div>
        <div class="heat-legend"><span>无成功</span><i /><i class="active" /><i class="strong" /><span>成功次数多</span><small>每格 30 分钟 · 更新于 {{ heatTimeline ? new Date(heatTimeline.updatedAt).toLocaleTimeString() : '--:--:--' }}</small></div>
      </section>

      <section class="metric-grid">
        <article class="metric"><span class="metric-icon blue"><Gauge :size="19" /></span><div><small>累计任务</small><strong>{{ stats.totalTasks }}</strong><em>全部通道</em></div></article>
        <article class="metric"><span class="metric-icon green"><Check :size="19" /></span><div><small>提链成功</small><strong>{{ stats.totalLinkSuccess }}</strong><em>{{ stats.successRate }}% 成功率</em></div></article>
        <article class="metric"><span class="metric-icon cyan"><Activity :size="19" /></span><div><small>当前执行</small><strong>{{ aggregate.running }}</strong><em>{{ aggregate.total }} 个任务项</em></div></article>
        <article class="metric"><span class="metric-icon amber"><CircleDollarSign :size="19" /></span><div><small>到账确认</small><strong>{{ stats.totalCallbackPaid }}</strong><em>异步回调</em></div></article>
      </section>

      <section class="workspace-grid" id="setup">
        <article class="panel account-panel">
          <div class="panel-head"><div><span class="section-kicker">ACCOUNT IMPORT</span><h2>导入账号</h2><p>每行一个 AT，导入后进入账号队列。</p></div><span class="count-badge">{{ accounts.length }} / 100</span></div>
          <textarea v-model="importText" rows="6" spellcheck="false" placeholder="每行粘贴一个 access token..." @input="scheduleImportParsing(importText)" />
          <div class="panel-actions">
            <button class="button primary" type="button" :disabled="importing" @click="importAccounts"><Upload :size="16" />{{ importing ? '解析中' : '导入到账号列表' }}</button>
            <button class="button secondary" type="button" @click="fileInput?.click()"><FileUp :size="16" />导入文件</button>
            <button class="button secondary" type="button" @click="fillSample"><KeyRound :size="16" />填入样例</button>
            <button class="button ghost" type="button" :disabled="!importText" @click="importText = ''"><Trash2 :size="16" />清空</button>
            <input ref="fileInput" type="file" accept=".txt,.json,.log" hidden @change="importFile" />
          </div>
        </article>

        <article class="panel run-panel">
        <div class="panel-head"><div><span class="section-kicker">RUN CONTROL</span><h2>运行控制</h2><p>参数只对本次 {{ currentMode?.name }} 任务生效。</p></div><span class="mode-pill">{{ currentMode?.name }}</span></div>
        <div class="control-grid">
          <label><span>并发 <b>{{ concurrency }}</b><small>上限 {{ concurrencyMax }}</small></span><input v-model.number="concurrency" type="range" min="1" :max="concurrencyMax" :aria-valuemax="concurrencyMax" /></label>
          <label><span>代理模式 <small v-if="proxyMode === 'builtin'" :class="{ 'pool-unavailable': !currentProxyPool?.enabled }">{{ currentProxyPool?.enabled ? `建单 ${currentProxyPool.primaryCount} / 出口 ${currentProxyPool.exitCount}` : '当前通道未配置' }}</small></span><select v-model="proxyMode"><option value="builtin">管理员代理池</option><option value="custom">自定义代理池</option></select></label>
          <label><span>国家 / 地区</span><select v-model="country"><option v-for="item in ['US','ID','PH','VN','IN','PL','BR','NL']" :key="item" :value="item">{{ item }}</option></select></label>
          <label><span>币种</span><select v-model="currency"><option v-for="item in ['USD','IDR','PHP','VND','INR','PLN','BRL','EUR']" :key="item" :value="item">{{ item }}</option></select></label>
          <label class="wide" v-if="proxyMode === 'custom'"><span>建单代理池 <small>主请求，每行一个</small></span><textarea v-model="proxiesText" rows="3" placeholder="http://host:port 或 socks5://user:pass@host:port" /></label>
          <label class="wide" v-if="proxyMode === 'custom'"><span>出口 / 账单代理池 <small>留空时回退建单代理</small></span><textarea v-model="exitProxiesText" rows="3" placeholder="每行一个出口代理" /></label>
          <div class="wide proxy-tools" v-if="proxyMode === 'custom'"><button class="button secondary" type="button" :disabled="proxyChecking" @click="testProxies"><TestTube2 :size="15" />{{ proxyChecking ? '检测中…' : '检测代理池' }}</button><span v-if="proxyChecks.length">成功 {{ proxyChecks.filter((item) => item.ok).length }} / {{ proxyChecks.length }}</span></div>
          <div class="wide proxy-check-results" v-if="proxyChecks.length"><div v-for="item in proxyChecks" :key="item.proxy" :class="{ ok: item.ok, fail: !item.ok }"><span>{{ item.ok ? '正常' : '失败' }}</span><code>{{ item.proxy }}</code><small>{{ item.ok ? `${item.ip || '未知出口'} · ${item.latencyMs}ms` : item.error }}</small></div></div>
          <div class="option-row wide">
            <label class="check-option switch-option"><input v-model="forceMethod" type="checkbox" role="switch" :aria-checked="forceMethod" /><span><b>强制支付方式</b><small>通道能力未回显时仍尝试提交</small></span><i class="switch-track" aria-hidden="true"><i /></i></label>
            <label class="check-option switch-option"><input v-model="forceZero" type="checkbox" role="switch" :aria-checked="forceZero" /><span><b>强压 0 元</b><small>金额未确认为 0 时仍继续</small></span><i class="switch-track" aria-hidden="true"><i /></i></label>
          </div>
          <label class="wide"><span>附加参数 <small>可选 JSON</small></span><input v-model="paramsText" type="text" placeholder='{"country":"US","currency":"USD"}' /></label>
        </div>
        <div class="run-actions">
          <button class="button launch" type="button" :disabled="!canSubmit" @click="createTask">
            <LoaderCircle v-if="submitting" :size="18" class="spin" /><Play v-else :size="18" />
            {{ submitting ? '正在提交' : `开始 ${currentMode?.name} 提链` }}
          </button>
          <span :class="{ 'run-hint-error': !canSubmit && !submitting }">{{ submitHint }}</span>
        </div>
        </article>
      </section>

      <section class="panel table-panel">
        <div class="panel-head"><div><span class="section-kicker">ACCOUNT QUEUE</span><h2>账号列表</h2><p>凭证仅用于服务端执行，页面只显示掩码。</p></div><button class="button ghost danger" type="button" :disabled="!accounts.length" @click="accounts = []"><Trash2 :size="15" />清空列表</button></div>
        <div class="data-table" v-if="accounts.length">
          <div class="table-row table-head"><span>#</span><span>账号</span><span>类型 / 算法</span><span>凭证</span><span>到期时间</span><span>状态</span><span></span></div>
          <div v-for="(account, index) in accounts" :key="`${account.atMask}:${index}`" class="table-row" :class="{ 'account-expired': account.expired }">
            <span class="row-index">{{ String(index + 1).padStart(2, '0') }}</span>
            <span class="account-name"><b>{{ account.email || '未识别账号' }}</b><small v-if="account.subject">{{ account.subject }}</small></span>
            <span class="token-meta"><b>{{ (account.type || 'plain').toUpperCase() }}</b><small>{{ account.alg || '无算法标记' }}</small></span>
            <code>{{ account.atMask }}</code>
            <span class="expiry">{{ expiryLabel(account.exp) }}</span>
            <span class="state" :class="account.expired ? 'failed' : 'ready'"><i />{{ account.expired ? '已过期' : '就绪' }}</span>
            <button class="icon-button" type="button" title="删除账号" @click="removeAccount(index)"><Trash2 :size="15" /></button>
          </div>
        </div>
        <div v-else class="empty-state"><UsersRound :size="28" /><strong>尚未导入账号</strong><span>粘贴 AT 后，账号会显示在这里。</span></div>
      </section>

      <section class="panel task-panel" id="tasks">
        <div class="panel-head"><div><span class="section-kicker">ASYNC TASKS</span><h2>任务记录</h2><p>仅显示当前 {{ currentMode?.name }} 通道的任务。</p></div><div class="panel-actions"><button class="button ghost danger" type="button" @click="clearAllTasks"><Trash2 :size="15" />清理任务</button><span class="count-badge">{{ visibleTasks.length }} 条</span></div></div>
        <div class="task-list" v-if="visibleTasks.length">
          <article v-for="task in visibleTasks" :key="task.id" class="task-row" :class="{ 'task-row-collapsed': !taskExpanded(task.id) }">
            <div class="task-main">
              <span class="task-channel">{{ task.modeName || task.mode }}</span>
              <div v-if="taskExpanded(task.id)"><strong>{{ task.id }}</strong><small><Clock3 :size="13" />{{ new Date(task.createdAt).toLocaleString() }}</small></div>
            </div>
            <div class="task-progress"><div><span :style="{ width: `${task.progress?.percent || 0}%` }" /></div><small>{{ task.progress?.done || 0 }} / {{ task.progress?.total || task.items?.length || 0 }}</small></div>
            <span class="state" :class="task.status"><i />{{ statusLabel(task.status) }}</span>
            <button v-if="taskExpanded(task.id) && ['queued','running','waiting_blik','waiting_plus'].includes(task.status)" class="button ghost danger" type="button" @click="cancelTask(task)"><Square :size="14" />停止</button>
            <span v-else-if="taskExpanded(task.id)" class="task-result-count">成功 {{ task.progress?.success || 0 }}</span>
            <button class="icon-button task-collapse-toggle" type="button" :title="taskExpanded(task.id) ? '折叠任务' : '展开任务'" :aria-expanded="taskExpanded(task.id)" @click="toggleTaskExpanded(task.id)"><ChevronLeft :size="15" :class="{ 'task-collapse-icon-open': taskExpanded(task.id) }" /></button>
            <button v-if="taskExpanded(task.id)" class="icon-button" type="button" title="查看任务事件" @click="openEvents(task)"><Activity :size="15" /></button>
            <div v-if="taskExpanded(task.id) && taskStatusHint(task)" class="task-status-hint">{{ taskStatusHint(task) }}</div>
            <div v-if="taskExpanded(task.id)" class="task-items">
              <div v-for="item in task.items" :key="item.index" class="task-item" :class="{ 'task-item-failed': isFailedTaskItem(item), 'task-item-expanded': itemDetailsOpen(task.id, item) }">
                <div class="task-item-head">
                  <div class="task-item-account"><strong>{{ item.email || `账号 ${item.index + 1}` }}</strong><span class="state" :class="item.status"><i />{{ statusLabel(item.status) }}</span></div>
                  <div class="task-item-actions">
                    <button class="button ghost task-details-toggle" type="button" :aria-expanded="itemDetailsOpen(task.id, item)" @click="toggleItemDetails(task.id, item.index)">{{ itemDetailsOpen(task.id, item) ? '收起详情' : '查看详情' }}</button>
                    <button v-if="item.email" class="icon-button" type="button" title="复制账号" @click="copyLink(item.email)"><Copy :size="14" /></button>
                    <button v-if="item.link" class="button ghost link-copy-button" type="button" title="复制链接" @click="copyLink(item.link)"><Copy :size="14" />复制链接</button>
                    <button v-if="item.extra?.qrImage || item.extra?.qr_image || item.qrImage" class="icon-button" type="button" title="查看二维码" @click="openQr(item)"><QrCode :size="14" /></button>
                  </div>
                </div>
                <div class="task-item-stage"><span class="stage-chip">阶段：{{ stageLabel(item.stage) }}</span><span class="stage-detail">{{ item.stageDetail || '正在等待 Worker 处理' }}</span></div>
                <div v-if="itemStatusHint(item)" class="task-status-hint">{{ itemStatusHint(item) }}</div>
                <div v-if="itemDetailsOpen(task.id, item)" class="task-error-detail" :class="{ 'task-error-detail-visible': isFailedTaskItem(item) }">
                  <div v-if="isFailedTaskItem(item)" class="task-error-title"><AlertCircle :size="15" />执行失败详情</div>
                  <div class="task-error-line"><strong>失败阶段</strong><span>{{ stageLabel(item.stage) }}</span></div>
                  <div v-if="item.stageDetail" class="task-error-line"><strong>阶段说明</strong><span>{{ item.stageDetail }}</span></div>
                  <div v-if="item.error" class="task-error-line"><strong>失败原因</strong><span>{{ item.error }}</span></div>
                  <div v-else-if="isFailedTaskItem(item)" class="task-error-line"><strong>失败原因</strong><span>执行引擎未返回具体原因，请查看任务事件。</span></div>
                  <div v-if="item.errorCode" class="task-error-line"><strong>错误码</strong><code>{{ item.errorCode }}</code></div>
                  <div v-if="item.attempts" class="task-error-line"><strong>尝试次数</strong><span>{{ item.attempts }} 次</span></div>
                </div>
                <div v-if="item.status === 'waiting_blik' || canRefreshQr(item)" class="task-item-controls">
                  <button v-if="item.status === 'waiting_blik'" class="button ghost blik-refresh" type="button" @click="submitBlik(task, item.index)">提交 BLIK</button>
                  <input v-if="item.status === 'waiting_blik'" v-model="blikCodes[`${task.id}:${item.index}`]" class="blik-input" inputmode="numeric" maxlength="6" placeholder="6 位动态码" @keyup.enter="submitBlik(task, item.index)" />
                  <button v-if="canRefreshQr(item)" class="button ghost blik-refresh" type="button" @click="refreshQr(task, item.index)">刷新二维码</button>
                </div>
              </div>
            </div>
          </article>
        </div>
        <div v-else class="empty-state"><Activity :size="28" /><strong>暂无 {{ currentMode?.name }} 任务</strong><span>验证 CDK 并提交账号后，当前通道的进度会显示在这里。</span></div>
      </section>
    </main>

    <transition name="toast">
      <div v-if="toast" class="toast" :class="toast.type"><Check v-if="toast.type === 'ok'" :size="17" /><AlertCircle v-else :size="17" />{{ toast.message }}</div>
    </transition>

    <transition name="modal" appear>
    <div v-if="fuseOpen" class="modal-backdrop" @click.self="fuseOpen = false">
      <section class="modal-panel fuse-modal">
        <div class="panel-head"><div><span class="section-kicker">CDK FUSION</span><h2>融合剩余次数</h2><p>来源 CDK 会永久失效，只保留新 CDK。</p></div><button class="icon-button" type="button" title="关闭" @click="fuseOpen = false"><X :size="18" /></button></div>
        <textarea v-model="fuseCodesText" rows="6" placeholder="输入至少 2 个 CDK，可换行或逗号分隔" />
        <input v-model="fuseRemark" maxlength="160" placeholder="融合备注（可选）" />
        <label class="fuse-confirm switch-option"><input v-model="fuseConfirmed" type="checkbox" role="switch" :aria-checked="fuseConfirmed" /><span>我确认来源 CDK 融合后不可恢复，正在执行的任务会导致融合失败。</span><i class="switch-track" aria-hidden="true"><i /></i></label>
        <div v-if="fusedCard" class="fuse-result"><span>新 CDK（仅显示一次，请立即保存）</span><strong>{{ fusedCard.code }}</strong><small>额度 {{ fusedCard.quota }} 次</small><button class="button primary" type="button" @click="copyFusedCard"><Copy :size="15" />复制新 CDK</button></div>
        <div class="run-actions"><button class="button secondary" type="button" @click="fuseOpen = false">取消</button><button class="button launch" type="button" :disabled="fuseSubmitting" @click="submitFuse">{{ fuseSubmitting ? '融合中…' : '确认融合' }}</button></div>
      </section>
    </div>
    </transition>
    <transition name="modal" appear>
    <div v-if="paymentOpen" class="modal-backdrop" @click.self="paymentOpen = false"><section class="modal-panel utility-modal"><div class="panel-head"><div><span class="section-kicker">RECHARGE</span><h2>在线充值</h2><p>充值模块已保留，支付通道由管理员配置。</p></div><button class="icon-button" type="button" title="关闭" @click="paymentOpen = false"><X :size="18" /></button></div><div v-if="paymentLoading" class="utility-loading"><LoaderCircle :size="20" class="spin" />正在读取充值配置</div><p v-else-if="utilityError" class="utility-error">{{ utilityError }}</p><div v-else-if="paymentEnabled && paymentPlans.length" class="payment-plans"><button v-for="plan in paymentPlans" :key="plan.id" class="button secondary" type="button">{{ plan.label }} · {{ plan.quota }} 次 / ¥{{ plan.money }}</button></div><div v-else class="empty-state"><CircleDollarSign :size="28" /><strong>在线支付暂未配置</strong><span>请联系管理员开通充值档位。</span></div></section></div>
    </transition>
    <transition name="modal" appear>
    <div v-if="plusOpen" class="modal-backdrop" @click.self="plusOpen = false"><section class="modal-panel utility-modal"><div class="panel-head"><div><span class="section-kicker">PLUS RECORDS</span><h2>Plus 到账记录</h2></div><button class="icon-button" type="button" title="关闭" @click="plusOpen = false"><X :size="18" /></button></div><div v-if="plusLoading" class="utility-loading"><LoaderCircle :size="20" class="spin" />正在读取到账记录</div><p v-else-if="utilityError" class="utility-error">{{ utilityError }}</p><div v-else class="record-list"><div v-for="record in plusRecords" :key="`${record.taskId}:${record.t}`"><strong>{{ record.email || '未知账号' }}</strong><small>{{ new Date(record.t).toLocaleString() }} · {{ record.taskId }}</small></div><div v-if="!plusRecords.length" class="empty-state"><ReceiptText :size="28" /><strong>暂无到账记录</strong></div></div></section></div>
    </transition>
    <transition name="modal" appear>
    <div v-if="eventsOpen" class="modal-backdrop" @click.self="eventsOpen = null"><section class="modal-panel utility-modal"><div class="panel-head"><div><span class="section-kicker">TASK EVENTS</span><h2>任务事件</h2><p>这里会保留 Worker 上报的阶段和失败原因。</p></div><button class="icon-button" type="button" title="关闭" @click="eventsOpen = null"><X :size="18" /></button></div><div v-if="eventsLoading" class="utility-loading"><LoaderCircle :size="20" class="spin" />正在读取任务事件</div><p v-else-if="utilityError" class="utility-error">{{ utilityError }}</p><div v-else class="record-list task-event-list"><div v-for="event in taskEvents" :key="event.eventId"><strong>{{ eventTypeLabel(event.type) }}</strong><small>{{ new Date(event.createdAt).toLocaleString() }} · {{ event.itemIndex == null ? '任务级' : `账号 ${event.itemIndex + 1}` }}</small><span v-if="event.payload?.stage" class="event-stage">阶段：{{ stageLabel(String(event.payload.stage)) }}</span><span v-if="event.payload?.detail" class="event-detail">{{ String(event.payload.detail) }}</span><span v-if="event.payload?.error" class="event-error">失败原因：{{ String(event.payload.error) }}</span><code v-if="event.payload?.error_code">错误码：{{ String(event.payload.error_code) }}</code></div><div v-if="!taskEvents.length" class="empty-state"><Activity :size="28" /><strong>暂无事件</strong></div></div></section></div>
    </transition>
    <transition name="modal" appear>
    <div v-if="qrOpen" class="modal-backdrop" @click.self="qrOpen = false"><section class="modal-panel utility-modal qr-viewer"><div class="panel-head"><div><span class="section-kicker">PAYMENT QR</span><h2>{{ qrTitle }}</h2><p>二维码只在当前任务返回时展示。</p></div><button class="icon-button" type="button" title="关闭" @click="qrOpen = false"><X :size="18" /></button></div><img :src="qrSrc" :alt="qrTitle" /><button class="button secondary" type="button" @click="copyLink(qrSrc)"><Copy :size="15" />复制二维码数据</button></section></div>
    </transition>
  </div>
</template>
