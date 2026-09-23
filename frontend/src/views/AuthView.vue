<script setup lang="ts">
import { ref } from "vue";
import { ArrowRight, KeyRound, LoaderCircle, LogIn, ShieldCheck, UserPlus } from "lucide-vue-next";
import { api, ApiError } from "../api/client";
import type { User } from "../types";

const emit = defineEmits<{ authenticated: [value: { token: string; user: User }] }>();
const mode = ref<"login" | "register">("login");
const username = ref("");
const password = ref("");
const displayName = ref("");
const busy = ref(false);
const error = ref("");

async function submit() {
  error.value = "";
  if (!username.value.trim() || !password.value) {
    error.value = "请输入用户名和密码";
    return;
  }
  busy.value = true;
  try {
    const result = mode.value === "login"
      ? await api.login({ username: username.value.trim(), password: password.value })
      : await api.register({ username: username.value.trim(), password: password.value, displayName: displayName.value.trim() });
    emit("authenticated", result);
  } catch (value) {
    error.value = value instanceof ApiError ? value.message : "请求失败，请稍后重试";
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <main class="auth-page">
    <section class="auth-panel">
      <div class="auth-icon"><ShieldCheck :size="25" /></div>
      <p class="auth-kicker">DJB WORKSPACE / SECURE ACCESS</p>
      <h1>{{ mode === 'login' ? '登录提链工作台' : '创建工作台账号' }}</h1>
      <p class="auth-copy">登录后管理自己的 CDK、账号队列和异步任务。每个 CDK 首次校验后只归属于当前用户。</p>
      <div class="auth-tabs"><button :class="{ active: mode === 'login' }" type="button" @click="mode = 'login'; error = ''"><LogIn :size="15" />登录</button><button :class="{ active: mode === 'register' }" type="button" @click="mode = 'register'; error = ''"><UserPlus :size="15" />注册</button></div>
      <label>用户名<input v-model="username" autocomplete="username" placeholder="3-64 位字母、数字或 _-." @keyup.enter="submit" /></label>
      <label v-if="mode === 'register'">显示名称<input v-model="displayName" autocomplete="name" placeholder="可选" /></label>
      <label>密码<input v-model="password" type="password" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" placeholder="至少 6 位" @keyup.enter="submit" /></label>
      <p v-if="error" class="auth-error">{{ error }}</p>
      <button class="button primary auth-submit" type="button" :disabled="busy" @click="submit"><LoaderCircle v-if="busy" :size="16" class="spin" /><ArrowRight v-else :size="16" />{{ busy ? '处理中…' : mode === 'login' ? '进入工作台' : '注册并登录' }}</button>
      <p class="auth-note"><KeyRound :size="14" />管理员入口仍在 <a href="/admin">管理后台</a>，用户账号不能进入后台。</p>
    </section>
  </main>
</template>

<style scoped>
.auth-page { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: radial-gradient(circle at 50% -10%, #203457 0, #111218 48%); }
.auth-panel { width: min(440px, 100%); padding: 34px; background: #1b1e27; border: 1px solid #303544; border-radius: 12px; box-shadow: 0 28px 90px rgba(0,0,0,.42); }
.auth-icon { width: 48px; height: 48px; display: grid; place-items: center; color: #8ab7ff; background: #17243a; border: 1px solid #2b4671; border-radius: 10px; }
.auth-kicker { margin: 17px 0 8px; color: #5799ff; font-size: 10px; font-weight: 800; letter-spacing: .9px; }.auth-panel h1 { margin: 0; font-size: 27px; }.auth-copy { margin: 10px 0 18px; color: #9099aa; font-size: 12px; line-height: 1.7; }
.auth-tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 18px; padding: 4px; background: #151820; border: 1px solid #272b36; border-radius: 7px; }.auth-tabs button { display: flex; justify-content: center; align-items: center; gap: 6px; padding: 9px; color: #9099aa; background: transparent; border: 0; border-radius: 5px; cursor: pointer; }.auth-tabs button.active { color: #fff; background: #263a5b; }
.auth-panel label { display: grid; gap: 7px; margin-top: 14px; color: #aab2c1; font-size: 11px; }.auth-panel input { width: 100%; }.auth-error { margin: 12px 0 0; color: #ff8793; font-size: 11px; }.auth-submit { width: 100%; margin-top: 17px; justify-content: center; }.auth-note { display: flex; align-items: center; gap: 6px; margin: 19px 0 0; color: #697285; font-size: 10px; line-height: 1.6; }.auth-note a { color: #8ab7ff; }
@media (max-width: 520px) { .auth-page { padding: 14px; }.auth-panel { padding: 25px 20px; } }
</style>
