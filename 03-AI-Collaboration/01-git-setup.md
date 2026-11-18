# 记录 01｜Git 建仓：三次误判，才把推送链路跑通

**日期**：2026-11-18
**协作对象**：WorkBuddy（AI 助手），本机 Windows 11
**一句话结论**：`git: command not found` 不等于"没装 git"；`Repository not found`
不等于"仓库没建"；`502` 不等于"被墙"。三次都是**表象指向了错误的原因**。

---

## 背景

Step 0 要求建公开仓库并持续提交。我此前完全没用过 Git，
所以让 AI 一步步带我走，我负责在 PowerShell 里执行和回报结果。

---

## 坑 1：`git` 找不到 —— 但 git 其实装着

**我的提问（原文）**：
> 为啥创建不了

**现象**：PowerShell 里 `where git` 什么都没有，`git --version` 报命令不存在。
AI 的判断是"这台机器没装 Git"，让我去下载 Git for Windows 安装包。

**AI 说错的地方**：
它只查了 `PATH` 环境变量就下结论。但 `PATH` 是**进程启动时的快照** ——
宿主进程（编辑器）比 Git 先启动，之后装的软件不会出现在它的 `PATH` 里。

**我怎么验证的**：
没装安装包，先查了三个地方：
1. 注册表里的 Machine PATH（`HKLM\SYSTEM\...\Environment`）
2. `C:\Program Files\Git\cmd\git.exe` 是否存在
3. 直接执行上面那个完整路径

结果：**`C:\Program Files\Git\cmd\git.exe` 存在，版本 2.56.0**，而且在 Machine PATH 里。
只有当前这个终端看不到。新开一个终端就正常了。

**最后怎么解决**：
写进脚本：所有需要 git 的地方先查 `PATH`，查不到再去
`C:\Program Files\Git\cmd\git.exe` 兜底。这样无论从哪个旧的终端启动都能跑。

**复盘**
下次遇到"某个命令找不到"，先问三句话：**是没装，还是没在 PATH 里，还是 PATH 是旧的？**
在没确认之前不要下载安装包 —— 差一步就会在系统里装出第二个 git，
以后 `where git` 返回两行、版本冲突，更难查。

---

## 坑 2：`Repository not found` —— 用户名少了一个字母

**现象**：仓库在网页上明明看得见，`git push` 报
`remote: Repository not found`。

**AI 说错的地方**：
它读我发的一张截图，把用户名读成了 `lihuany0247`，还"顺手"
把 remote 地址改成了那个错的拼写 —— 于是本来能成的操作被改坏了。

**我怎么验证的**：
不看截图，直接打 GitHub API 对照：

```powershell
python -c "import urllib.request,json; print(json.load(urllib.request.urlopen('https://api.github.com/users/lihuanyi0247'))['login'])"
```

返回 `lihuanyi0247`（多一个 `i`）。

**最后怎么解决**：
`git remote set-url origin https://github.com/lihuanyi0247/ICEC-2026-0247-lihuanyi.git`

**复盘**
**判断拼写不要靠 OCR / 肉眼，要靠 API 返回值。**
`git ls-remote <地址>` 也能当对照 —— 它成功就说明地址对，失败就说明地址错，
一次调用就能把"网络问题"和"地址问题"分开。

---

## 坑 3：`CONNECT tunnel failed, response 502` —— 不是墙，是残留代理

**现象**：

```
CONNECT tunnel failed, response 502
```

**AI 的第一判断是错的**：它以为是被墙，让我换镜像站、改 SSH、甚至考虑换 Gitee。

**我怎么验证的**：
分三步把它拆开：

1. 看环境变量：`$env:HTTP_PROXY` / `$env:HTTPS_PROXY` 指向 `127.0.0.1:7897`
2. 测那个端口是否真的在转发：`Test-NetConnection 127.0.0.1 -Port 7897`
3. 清空代理变量后重试

第 3 步一清就通了。**根因是环境里残留了代理变量，而代理进程没在正常转发。**

**然后是第二个更隐蔽的问题**：清空代理后，`github.com` 解析到
`57.155.223.240`，这个 IP 完全连不上（21 秒超时）；
但 `api.github.com` 正常，`140.82.112.3` 这些边缘 IP 直连也正常。
→ 是**单个域名的 DNS 解析被投毒**，不是整体不通。

**最后怎么解决**：
写了个一次性小工具 `gh_push.py`：先正常 push；失败就起一个本地 CONNECT 代理，
把 `github.com` 的流量改写到能通的边缘 IP，推完立刻退出。
**不改 hosts、不改全局 git 配置** —— 因为改 hosts 是全局副作用，
今天能通的 IP 明天可能就变了，留个坑给自己。

**复盘**
报错信息里的关键词经常指向**错误的方向**。我现在的顺序是：
**先分层定位（DNS → TCP → TLS → HTTP → 应用），再动手改。**
"502" 是 HTTP 层的错误，说明 TCP 已经通了 —— 当时就应该想到是代理而不是墙。

---

## 这段记录能证明什么

- 会**拒绝 AI 的错误建议**（没装安装包、没换平台、没改 hosts）
- 会用**可执行的验证**代替猜测（API 对照、分层探测、对照组）
- 每次误判后都留下了**可复用的修复**（脚本兜底、写法固化）
