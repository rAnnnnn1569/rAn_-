# 前端白屏 / 模板不渲染 —— 排查 SOP

> 适用：Vue + Element UI 前端页面显示 `{{ }}` 原文、组件没渲染、样式全丢。

---

## 一、30 秒定性（先看现象）

| 现象 | 结论 |
|---|---|
| 满屏 `{{ xxx }}` 原样输出 + `v-if` 失效（多块内容同时显示）+ `<el-xxx>` 没变组件 | **Vue 没运行** → 往下走 |
| 页面正常，只是数据是空的 | 后端接口问题，与本文无关 |
| 纯空白页 | 先确认 index.html 本身是否 200 |

---

## 二、定位（F12 三步）

**1. Console 看红字**

| 报错 | 含义 |
|---|---|
| `Vue is not defined` | Vue 没加载 |
| `Cannot read properties of undefined (reading 'prototype')` | Element UI 连带炸，根因仍是 Vue 没加载 |
| `SyntaxError` | app.js 语法不兼容（如可选链 `?.` 在旧浏览器） |

**2. Network 看静态资源**

| 状态 | 原因 |
|---|---|
| `ERR_CONNECTION_RESET` / `ERR_NAME_NOT_RESOLVED` / 超时 | **外网 CDN 不通** |
| `404` | **文件路径不对** |

**3. 一句话二分**：地址栏直接开 `http://<平台地址>/static/js/app.js`

- 能显示 JS 内容 → 文件在，是 **CDN 问题**
- 404 → 是 **路径问题**

> 判据：app.js 能在 Console 报错，说明它自己加载成功了，别再怀疑路径。

---

## 三、修复

### A. CDN 不通（内网 / 无外网，最常见）

**外链一律本地化，不依赖 jsdelivr、unpkg**——内网机出不去。

1. 建 `frontend/vendor/`
2. 从国内可达源下载（bootcdn 实测可用）：

```
https://cdn.bootcdn.net/ajax/libs/vue/2.6.14/vue.min.js
https://cdn.bootcdn.net/ajax/libs/element-ui/2.15.14/index.js
https://cdn.bootcdn.net/ajax/libs/element-ui/2.15.14/theme-chalk/index.css
https://cdn.bootcdn.net/ajax/libs/element-ui/2.15.14/theme-chalk/fonts/element-icons.woff
https://cdn.bootcdn.net/ajax/libs/element-ui/2.15.14/theme-chalk/fonts/element-icons.ttf
https://cdn.bootcdn.net/ajax/libs/axios/1.6.0/axios.min.js
```

> 字体必须一起下（`fonts/` 放 `vendor/` 下），漏了图标全变方框。

3. `index.html` 改成：

```html
<link rel="stylesheet" href="/static/vendor/element-ui.css">
<link rel="stylesheet" href="/static/css/style.css">
<script src="/static/vendor/vue.min.js"></script>
<script src="/static/vendor/element-ui.js"></script>
<script src="/static/vendor/axios.min.js"></script>
```

**顺序不能乱**：Vue → Element UI → axios → app.js

4. 查残留：`grep -n "http" index.html` —— 应无任何结果

### B. 路径 404

`app.mount("/static", StaticFiles(directory=frontend_dir))` 的规则：
`/static/a/b.css` → `frontend/a/b.css`

**必须带子目录**：

```
frontend/
├── index.html
├── css/style.css
├── js/app.js
└── vendor/
```

平铺成 `frontend/style.css` 就 404。

---

## 四、验收（三项全过才算完）

1. **URL 映射逐条核**：index.html 里每个 `/static/...` 都要在磁盘上找到对应文件
2. **Console 零报错**
3. **Ctrl+F5 硬刷新**

> StaticFiles 实时读盘，**不用重启服务**；白屏反复出现八成是浏览器缓存。

---

## 五、踩过的坑

| AI说不用重启，但是我是重启了续费操作才能正常进行 |      |
|---|---|
|                                                  |      |
|                                                  |      |
|                                                  |      |
|                                                  |      |
|                                                  |      |
