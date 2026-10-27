# SignalToday

[English](README.md)

SignalToday 是一个以"少刷一点，多知道一点"为原则打造的、移动优先的信息雷达。

本仓库目前只包含 Stage 1 的基础设施。采集、AI生成、聚类、评分、配图、邮件发送、用户账户、公开部署等功能会在后续阶段陆续加入。

## 本地开发

前置依赖：Docker Engine 和 Docker Compose。

```bash
cp .env.example .env
# 启动前把 .env 里每一个必填占位符都替换掉。
docker compose build
docker compose run --rm web python manage.py migrate
docker compose up -d
docker compose ps
```

本地健康检查地址是 `http://127.0.0.1:8000/health/`，Django 管理后台在 `http://127.0.0.1:8000/admin/`。

运行测试套件：

```bash
docker compose run --rm web python manage.py test
```

## 架构

- `web`：Django 5.2 LTS，由 Gunicorn 提供服务。
- `scheduler`：独立进程，使用同一个应用镜像运行 APScheduler。
- `postgres`：PostgreSQL 17，启用了 `pg_trgm`。

PostgreSQL 使用 Docker 的 named volume，存放在 Docker 本地数据根目录下。不要把它指向网络/CIFS/NFS 挂载点作为在线数据目录。

## 许可证

MIT。详见 [LICENSE](LICENSE)。

## 内容与信源免责声明

SignalToday 会从你配置的第三方信源（见 `config/sources/`）抓取标题和元数据。它的设计目标是总结并回链到原始报道，而不是转载第三方文章全文。抓取内容的版权归原作者/原发布方所有。

如果你自行部署本项目，你需要自行承担所选信源的服务条款、版权法律以及所在司法辖区相关法规的合规责任。本项目不提供任何担保，也不对他人部署或运营本项目产生的后果承担责任，完整免责条款见 LICENSE。
