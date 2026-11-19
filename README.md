# Fossil 的 LLVM BOLT 跳转表识别优化

先按 [新机器复现指引](docs/REPRODUCE.md) 操作；原理与原始 WSL 实验见 [设计报告](REPORT.md)。当前复现版本为 **repro-v2-20261120**，修正了跨机器负对照的 ELF 元数据误报，并增加环境预检、逐阶段日志和失败保留。

- [代码补丁](patches/notrack-jump-table-options.patch) 新增默认关闭的 `--x86-notrack-jump-tables`，含 BOLT 回归测试。
- [Dockerfile](docker/Dockerfile) 定义原环境；[v2 Dockerfile](docker/Dockerfile.reproduction) 在固定原镜像上更新复现工具，保留原 BOLT 和测试负载。
- [证据目录](evidence/mainline-options/af3fdf39125a) 保存逐轮计时、功能校验、训练数据和编译测试记录。

大型镜像和匹配材料包在 [repro-v2 Release](https://github.com/zhenghaoming1111/fossil-bolt-notrack/releases/tag/repro-v2-20261120) 提供；Git clone 不包含 Docker 归档。下载后校验 SHA-256、`docker load`，再从 Bash 使用统一运行入口，无需重新编译 LLVM。

- [新环境排错](docs/TROUBLESHOOTING.md)：Docker、网络、镜像摘要、PMU、CPU 与权限问题。
- [服务器验证](docs/SERVER_VALIDATION.md)：KVM 配置、实测结果及原复现过程中的修正边界。
