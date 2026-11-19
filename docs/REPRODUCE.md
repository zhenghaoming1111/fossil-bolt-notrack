# 在一台新机器上复现

本指引对应 **repro-v2-20261120**。它把 2026-11-19 在另一台 KVM 服务器上遇到的校验问题修入正式脚本。LLVM 补丁、固定主线、数据集、训练与计时方案保持原样。旧镜像 `main-af3fdf39125a` 不包含这项脚本修正，不能与 v2 源码混用。

## 1. 准备机器

- x86-64 Linux，或 x86-64 WSL2 的 Linux 终端。Docker 必须运行 Linux 容器；ARM 模拟运行不用于性能结论。
- Bash、Python 3、Docker CLI 和已经启动的 Docker daemon；至少两个允许调度的逻辑 CPU。
- 结果文件系统至少留 8 GiB 空间，Docker 数据目录也需留出镜像解压空间；建议可用内存 8 GiB 以上。LLVM 源码编译另计。
- 结果保存在 Linux 文件系统。WSL 使用 `~/`，避免放在 `/mnt/c`。

Docker 尚未安装时，按 [Docker 官方 Linux 安装指引](https://docs.docker.com/engine/install/)准备；预检不会自动安装软件、删除 apt 锁、终止系统更新或调整内核。若系统正在更新，等它完成再正式计时。普通账号不能连接 Docker 时，先执行 `sudo -v`，再在下文的运行入口增加 `--sudo`；不要改用 `sudo python3` 运行整个实验。

KVM/WSL 不一定开放硬件 perf 事件。本实验用 **BOLT 插桩**采集 profile，硬件 PMU、LBR 和 perf 权限不属于必需条件。缺失时不报告 IPC、硬件分支失误率等指标。

## 2. 取得成套交付文件

从本仓库的 [repro-v2-20261120 Release](https://github.com/zhenghaoming1111/fossil-bolt-notrack/releases/tag/repro-v2-20261120) 下载下面四个文件，放在同一个目录：

1. `fossil-bolt-image-repro-v2-af3fdf39125a.tar.gz`：可直接加载的完整镜像。
2. `fossil-bolt-materials-repro-v2-af3fdf39125a.tar.gz`：与镜像匹配的源码、文档和证据。
3. `DELIVERY_SHA256SUMS`：两个归档及镜像说明文件的 SHA-256。
4. `image-manifest.json`：平台、归档摘要、配置摘要和层摘要。

Git 仓库不保存大型 Docker 归档；**只下载 GitHub 的 Source code 压缩包不会得到镜像**。服务器不能连接 GitHub/Docker Hub 时，在可联网的电脑下载后用 `scp` 或其他文件传输工具送到服务器；下面的步骤离线执行，不会拉取其他镜像。

在 Bash 中进入这四个文件所在目录，然后执行：

```bash
sha256sum --check DELIVERY_SHA256SUMS
docker load --input fossil-bolt-image-repro-v2-af3fdf39125a.tar.gz
tar -xzf fossil-bolt-materials-repro-v2-af3fdf39125a.tar.gz
cd fossil-bolt-submission
results=$(mktemp -d "$HOME/fossil-bolt-results.XXXXXXXX")
printf '结果目录：%s\n' "$results"
```

Docker 需要管理员权限时，仅把加载命令改成 `sudo docker load ...`。

## 3. 先预检，再校验

定义本次运行入口；后续均在同一个 Bash 会话中执行：

```bash
run_fossil() {
  bash scripts/run_reproduction.sh "$@" --results "$results"
}

run_fossil preflight
run_fossil verify
run_fossil check
run_fossil negative-check
```

若需要 sudo，把函数内那一行改成 `bash scripts/run_reproduction.sh "$@" --results "$results" --sudo`，并先运行 `sudo -v`。已有项目专用 Docker 启动器时可加 `--docker /absolute/path/to/run-docker`；也可按 Docker 自身方式设置 `DOCKER_HOST`。脚本不会改变系统 Docker 配置。

预检会验证主机架构、CPU 数量、剩余空间、daemon 连接、镜像平台与版本、本地文件和镜像内的校验清单是否一致、普通用户能否写入结果目录，以及 BOLT/Fossil 能否执行。默认读取材料目录旁的 `image-manifest.json` 核对完整 RootFS 层；文件另存时加 `--image-manifest /完整路径/image-manifest.json`。缺少该文件会记录未核对完整层身份的警告。镜像 ID 显示格式可能随 Docker 存储后端变化；不能仅凭该字符串变化就断定镜像换了，见 [排错说明](TROUBLESHOOTING.md)。

`verify` 复算原先提交的 WSL/Docker 证据和 2026-11-19 的服务器证据；`check` 则会**新插桩、新训练、新重写**，检查 A/B/C 页面、跳转表和前缀。`negative-check` 从冻结源码重新构建不含目标指令的 Fossil，验证开关不改变执行代码和数据。它会留下 `runtime-equivalence.json`，区分原始字节是否相同和经过限定元数据核对后是否等价。

## 4. 正式计时

前面的检查通过后，避免并行编译和系统更新，依次运行：

```bash
run_fossil benchmark
run_fossil negative
find "$results" -name summary.json -print
```

也可用 `run_fossil all` 从预检后按顺序执行上述五个阶段。完整运行可能需要两小时或更久，取决于 CPU 及请求量校准。每个阶段建立新目录并记录完整命令、退出码和控制台日志；不会覆盖旧结果。

- 正向：三个场景 × 两个评估种子 × 24 轮 × A/B/C，共 432 条正式计时。训练种子为 42，评估种子为 43/44；B/C 共用新生成的同一 profile，只切换选项。
- 负对照：48 轮 OFF/ON，共 96 条正式计时，预先固定 1% 性能回退界限。
- CPU 从当前容器允许的列表自动选前两个，不要求 CPU 必须叫 0、1，也不需要手动设置 `CPU_ID`。
- 活跃的 apt/dpkg 或编译进程会使正式计时停止；脚本不自动暂停、杀死或恢复其他进程。

## 5. 找日志和判断结果

结果根目录中的 `host-preflight-*.json` 保存每次环境快照；`host-preflight.json` 是最新快照。每阶段目录包含 `execution.json`、`console.log`，实验结果在其 `output/` 子目录。查看日志可直接使用 `less -R /完整路径/console.log`。

校验必须通过且请求错误为零。新计时报告逐轮配对的耗时下降中位数、区间、胜平负和波动；不要求陌生机器与原机恰好得到相同百分比。虚拟机上的结果应写为虚拟机测量，不能称为裸机测量。数据和页面比例保持冻结，不能为追求更好的结果临时换请求或删轮次。

失败时保留该目录及退出码。修正原因后用新目录重跑相应阶段，不手工修改已有 `raw.jsonl`，也不在已有正式数据上套用旧的服务器临时恢复脚本。首次服务器复现和修正后的交付验收分别记录，见 [跨机器验证](SERVER_VALIDATION.md)。
