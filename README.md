# huaweicloud-logs

使用华为云官方 Python SDK 查询 LTS 日志的通用 Skill，适用于 **Codex、DeepSeek Harness（dsh）和 Claude Code**。

在项目中用自然语言描述查询条件，Skill 自动选择日志组和日志流，并直接返回日志原文。默认不分析业务问题、不生成日志文件；需要上下文或导出时再明确提出。

## 功能

- 按时间、LTS 搜索表达式和标签筛选日志，支持分页查询。
- 支持 AK/SK、临时安全凭证和 IAM 用户名密码认证。
- 自动发现日志组和日志流；目标不唯一时查询多个候选流，不固定业务日志组。
- 可从当前项目源码中的日志语句提取查询关键词，只读检索，不执行项目代码。
- 按需查询相邻日志，或导出为 JSON、JSONL、文本。

## 安装

### npx 安装（推荐）

需要 Node.js 22.20+ 和 Git。使用 [Skills CLI](https://github.com/vercel-labs/skills) 从本仓库安装，按提示选择编程助手：

```bash
npx skills@latest add bingodfok/huaweicloud-logs -g
```

也可以直接指定宿主：

```bash
# Codex
npx skills@latest add bingodfok/huaweicloud-logs -g -a codex

# Claude Code
npx skills@latest add bingodfok/huaweicloud-logs -g -a claude-code
```

`-g` 表示用户级安装，省略则安装到当前项目；`--copy` 可改为复制文件。Skills CLI 当前为 Codex 使用 `~/.agents/skills/`，Claude Code 使用 `~/.claude/skills/`，与下方手动安装路径可能不同。已有安装先确认其实际位置与来源，选择一种方式维护。

查看可安装技能或检查用户级安装：

```bash
npx skills@latest add bingodfok/huaweicloud-logs --list
npx skills@latest list -g
```

已通过 Skills CLI 安装的技能，可用 `npx skills@latest update huaweicloud-logs -g` 更新。dsh 尚未列入该 CLI 的支持名单，使用下方手动安装方式。参数与宿主支持情况以 [CLI 文档](https://github.com/vercel-labs/skills/blob/main/docs/commands.md) 和 [宿主列表](https://github.com/vercel-labs/skills/blob/main/docs/agents.md) 为准。

### 让 Agent 安装

把这段话复制给 Codex 或 Claude Code：

```text
请按 https://github.com/bingodfok/huaweicloud-logs 的安装说明，使用 npx skills@latest add bingodfok/huaweicloud-logs -g 安装到当前编程助手，并验证安装结果。
```

使用 dsh 或未准备 Node.js 时，可让 Agent 按下方手动安装说明完成安装。

### 手动安装

也可以在终端安装。将完整仓库放入所用宿主的用户级技能目录，目录名为 `huaweicloud-logs`，保留 `SKILL.md`、`scripts/` 等文件。

| 宿主 | 默认用户级安装目录 | 调用方式 |
| --- | --- | --- |
| Codex | `~/.codex/skills/huaweicloud-logs/` | `$huaweicloud-logs 查询……` |
| dsh | `~/.dsh/skills/huaweicloud-logs/` | `/huaweicloud-logs 查询……` |
| Claude Code | `~/.claude/skills/huaweicloud-logs/` | `/huaweicloud-logs 查询……` |

以 Codex 为例，首次安装时执行：

**Windows PowerShell**

```powershell
git clone https://github.com/bingodfok/huaweicloud-logs.git (Join-Path $HOME '.codex/skills/huaweicloud-logs')
```

**macOS / Linux**

```bash
git clone https://github.com/bingodfok/huaweicloud-logs.git "$HOME/.codex/skills/huaweicloud-logs"
```

其他宿主替换目标目录即可。自定义宿主目录时使用实际技能路径，dsh 设置了 `DSH_HOME` 时使用其下的 `skills/`。详细宿主约定见 [SKILL.md](SKILL.md)。

### 安装约定

- 使用仓库当前默认分支，仓库根目录就是完整 Skill 包。使用 Codex 的 GitHub Skill 安装器时，先确认默认分支并通过 `--ref` 显式指定，同时设置 `--path . --name huaweicloud-logs`。
- 已有同名安装先核对来源和本地改动，再安全更新，保留本机配置与本地修改。
- 安装仅放置 Skill 文件；工作目录和共享依赖在实际查询时按 [SKILL.md](SKILL.md) 准备。
- 安装验证只检查路径、`SKILL.md`、`scripts/lts.py`、`scripts/requirements.txt` 及宿主识别情况，告知安装版本和调用方式。验证时不读取凭证、不创建运行环境、不执行日志查询。

## 首次使用准备

- 本机有 Python 3.10+，宿主能够读取项目文件并执行本地命令。
- 网络可访问华为云 IAM、LTS，以及首次准备依赖时使用的软件包源。
- 准备对应区域的 `project_id`，以及具有 API 访问和日志读取权限的凭证。

真实执行查询时，宿主按照 Skill 指令创建或复用共享虚拟环境，并按 [requirements.txt](scripts/requirements.txt) 安装官方 SDK。缺少 Python、网络或命令执行能力时会报告准备失败，不会自动安装 Python。

### 认证

凭证通过本机环境变量提供，不放入仓库、查询配置或命令参数。

| 认证方式 | 环境变量 |
| --- | --- |
| AK/SK（默认） | `HUAWEICLOUD_SDK_AK`、`HUAWEICLOUD_SDK_SK` |
| 临时 AK/SK | 上述两项，加 `HUAWEICLOUD_SDK_SECURITY_TOKEN` |
| IAM 用户名密码 | `HUAWEICLOUD_SDK_USERNAME`、`HUAWEICLOUD_SDK_PASSWORD`、`HUAWEICLOUD_SDK_DOMAIN_NAME` |

`HUAWEICLOUD_SDK_DOMAIN_NAME` 是 IAM 用户所属的华为云账号名。使用密码认证时将配置中的 `auth` 设为 `password`；认证方式不会自动回退。密码换取的 IAM Token 只保存在进程内存中，当前不支持需要 MFA 的 API 登录保护。

Windows 可使用当前用户级环境变量。脚本优先读取当前进程中的整组凭证；该组变量全部未设置时，才读取 Windows 用户级变量，不混用两个来源。macOS、Linux 和 WSL 使用各自进程的环境变量。

### 项目配置

可在项目根目录或用户主目录放置 `.huaweicloud-logs.json`，用真实项目 ID 替换示例值：

```json
{
  "auth": "aksk",
  "region": "cn-southwest-2",
  "project_id": "<project-id>",
  "timezone": "+08:00"
}
```

默认区域为 `cn-southwest-2`，默认时区为 `+08:00`。日志组、日志流可以不写入配置，由 Skill 按本次请求选择。

配置文件依次选取：`--config` 指定文件 → 项目根目录配置 → 用户主目录配置；只选一个，不合并。命令行参数覆盖配置值。本机配置文件已加入仓库的忽略规则。

## 查询示例

在业务项目中调用：

```text
$huaweicloud-logs 查询最近 24 小时订单导入失败的日志
$huaweicloud-logs 查询最近 1 小时 requestId=abc123 的日志
$huaweicloud-logs 查询这条日志前后各 20 条记录
$huaweicloud-logs 将刚才的查询结果导出为 JSONL
```

dsh 和 Claude Code 将前缀替换为 `/huaweicloud-logs`。未指定时间范围时默认查询最近 1 小时。

有匹配时直接返回日志原文；全部目标查询完成且无匹配时回复“未查到”。失败或未完成会明确说明，不当作无匹配。默认不附加业务分析，日志文件仅在明确要求导出时生成。

## 工作目录与依赖

| 路径 | 用途 |
| --- | --- |
| `<项目>/.huaweicloud-logs/` | 查询进程工作目录，`cache/` 和 `tmp/` 按需存放缓存、临时文件 |
| `~/.huaweicloud-logs/venv/` | 跨项目共享的 Python 虚拟环境与 SDK 依赖 |

这些目录仅在实际执行日志任务时按需创建，安装、同步或讨论方案时不预先创建。项目源码检索和项目配置仍以项目根目录为准；依赖不会安装到业务项目或系统 Python 中。

## 脚本与维护

[scripts/lts.py](scripts/lts.py) 提供 `resources`（列出资源）、`query`（单日志流查询）和 `context`（相邻日志）三个命令。自动选择多个日志流、提取源码关键词和准备运行环境由宿主按 Skill 指令完成。

直接调用脚本时，使用共享环境的 Python 和脚本绝对路径；单次 `query` / `context` 需要日志组和日志流 ID。脚本默认向 stdout 输出 JSON，Skill 从中提取日志后回复。完整参数、分页状态和导出约定见 [SKILL.md](SKILL.md)。

在已具备依赖的 Python 环境中，从仓库根目录运行离线测试：

```text
python -m unittest discover -s tests -v
```

测试使用模拟请求，不需要真实云凭证。
