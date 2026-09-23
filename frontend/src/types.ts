export type Channel = {
  id: string;
  name: string;
  description?: string;
  color?: string;
};

export type TaskItem = {
  index: number;
  email?: string;
  atMask?: string;
  status: string;
  stage?: string;
  stageDetail?: string;
  error?: string | null;
  errorCode?: string | null;
  link?: string | null;
  attempts?: number;
  durationMs?: number | null;
  startedAt?: number | string | null;
  finishedAt?: number | string | null;
  extra?: Record<string, unknown>;
  qrImage?: string | null;
};

export type Task = {
  id: string;
  mode: string;
  modeName?: string;
  status: string;
  createdAt: number | string;
  updatedAt?: number | string | null;
  finishedAt?: number | string | null;
  items: TaskItem[];
  progress?: {
    total: number;
    done: number;
    running: number;
    queued: number;
    success: number;
    failed: number;
    percent: number;
  };
  logs?: Array<{ t: number; level: string; msg: string }>;
};

export type MetaResponse = {
  app: string;
  version: string;
  modes: Channel[];
  engine: {
    defaultConcurrency: number;
    maxConcurrency: number;
    proxyPools?: Record<string, { enabled: boolean; primaryCount: number; exitCount: number }>;
    ha?: { enabled: boolean; mode: string; ready: boolean };
  };
  user?: { taskConcurrency?: number };
  limits?: { userConcurrency?: number; cdkConcurrency?: number; maxConcurrency?: number };
  serverTime: number;
};

export type StatsResponse = {
  totalTasks: number;
  totalLinkSuccess: number;
  totalFailed: number;
  totalCallbackPaid: number;
  successRate: number;
};

export type AdminOverview = {
  tasks: { total: number; queued: number; running: number; success: number; failed: number };
  items: { total: number; success: number; failed: number };
  cards: { total: number; active: number; quota: number; used: number; reserved: number };
};

export type AdminCard = {
  id: number;
  code?: string;
  codeMask: string;
  quota: number;
  usedQuota: number;
  reservedQuota: number;
  remain: number;
  status: string;
  fusionDisabled?: boolean;
  ownerUserId?: number | null;
  ownerUsername?: string | null;
  boundAt?: string | null;
  remark?: string;
  createdAt: string;
  updatedAt: string;
};

export type AdminTask = {
  id: string;
  ownerMask: string;
  cardCode?: string;
  mode: string;
  modeName: string;
  status: string;
  concurrency: number;
  itemCount: number;
  successCount: number;
  failedCount: number;
  createdAt: string;
  updatedAt: string;
};
export type AdminUser = User & { taskCount: number; cardCount: number; online: boolean };
export type User = { id: number; username: string; displayName: string; role: string; status: string; taskConcurrency: number; maxAccountsPerTask: number; allowFusion: boolean; notes?: string; createdAt: string; lastLoginAt?: string | null };
export type UserPolicy = { registrationEnabled: boolean; maxUsers: number; defaultConcurrency: number; maxAccountsPerTask: number; allowFusion: boolean };

export type ClusterStatus = {
  mode: string;
  database: string;
  redisConfigured: boolean;
  kafkaConfigured: boolean;
  consumerGroup: string;
  topics: { commands: string; events: string; dlq: string };
};

export type AdminConcurrency = { default: number; vip: number; globalLimit: number };
export type AdminProxyPool = { enabled: boolean; primaryText: string; exitText: string };
export type AdminProxyPools = { channels: Record<string, AdminProxyPool> };
export type AdminAnnouncement = { id?: number; title: string; content: string; updatedAt?: string };
export type AdminLog = { id: number; level: string; action: string; message: string; mode?: string | null; taskId?: string | null; payload?: Record<string, unknown>; createdAt: string };
export type AdminRouteStat = { mode: string; total: number; success: number; failed: number; running: number; statuses: Record<string, number> };

export type SuccessTimeline = {
  updatedAt: number;
  buckets: number;
  modes: Record<string, string>;
  totals: { all: number; byMode: Record<string, number> };
  cells: Array<{ start: number; end: number; total: number; byMode: Record<string, number> }>;
};
