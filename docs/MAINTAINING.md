# 维护交付版本

运行实验按 [复现指引](REPRODUCE.md)。下列命令仅用于维护者更新交付，均在仓库根目录的 Linux Bash 中执行。

先审阅修改，运行单元测试和原始数据复核；不要修改历史计时文件来适应新脚本：

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -O -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/verify_fossil_submission.py
python3 scripts/verify_server_results.py
python3 scripts/update_source_checksums.py
python3 scripts/update_source_checksums.py --check
sha256sum --check SOURCE_SHA256SUMS --quiet
```

重建 v2 时先加载留存的原始镜像，作为已冻结的依赖和二进制来源：

```bash
docker load --input /path/to/fossil-bolt-image-af3fdf39125a.tar.gz
docker build --pull=false --network none \
  -f docker/Dockerfile.reproduction \
  -t bolt-fossil-notrack:repro-v2-af3fdf39125a .
```

这一步无需联网安装依赖或重编 BOLT。基础环境从零构造的方式仍由原 `docker/Dockerfile` 记录。构建完成后按正式入口运行预检、`verify`、`check`、`negative-check`、`negative`，保留成功和失败日志。未重跑的正向计时应继续引用其原日期，不能换成新版本的测量标签。变更计时器、BOLT 或负载时，需要重新做正向正式实验。

源码冻结并完成验收后导出新交付目录：

```bash
python3 scripts/package_delivery.py --output "$HOME/fossil-bolt-delivery-v2"
cd "$HOME/fossil-bolt-delivery-v2"
sha256sum --check DELIVERY_SHA256SUMS
```

使用项目独立 Docker 启动器时，将构建命令的 `docker` 替换为该启动器，导出脚本加 `--docker /absolute/path/to/run-docker`。不得把个人 socket 路径写进通用配置。

导出脚本从实际 Docker 归档读取 config 并计算摘要，分别记录 Docker 显示 ID、config digest 和 RootFS diff IDs，避免将不同类型的摘要混为一谈。发布时把完整镜像、材料、SHA-256 清单和 `image-manifest.json` 作为**同一 GitHub Release** 的附件，并让标签指向匹配的源代码提交。大型归档保留在 Release，不强行加入 Git 历史。

CI 会检查源码清单、校验器测试、Bash 语法和两台环境的归档统计；CI 不代表已经在 GitHub runner 上重做完整性能实验。`release-metadata/` 保存交付验收日志，单独排除在镜像和源码清单之外，避免镜像摘要和自身记录形成循环依赖。
