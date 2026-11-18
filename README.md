# Fossil 的 LLVM BOLT 跳转表识别优化

从 [设计与复现报告](REPORT.md) 开始。报告给出实验设备、三组对照、无命中负对照和可直接执行的 Bash 复现命令。

- [代码补丁](patches/notrack-jump-table-options.patch) 新增默认关闭的 `--x86-notrack-jump-tables`，含 BOLT 回归测试。
- [Dockerfile](docker/Dockerfile) 定义实验环境；实际导出的 Docker 镜像随交付包提供。
- [证据目录](evidence/mainline-options/af3fdf39125a) 保存逐轮计时、功能校验、训练数据和编译测试记录。

离线复现实验使用随交付包提供的 `fossil-bolt-image-af3fdf39125a.tar.gz`，从 Bash 执行 `docker load` 即可开始，无需重新编译 LLVM。
