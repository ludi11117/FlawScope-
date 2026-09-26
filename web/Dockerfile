# FlawScope 前端容器：构建静态产物 + nginx 托管并反代 API。
#
# 为什么用两阶段构建：
#   第一阶段需要 node（编译 TS/打包），第二阶段只需要 nginx。
#   合并成单阶段会把 node_modules（数百 MB）带进最终镜像，
#   而它对运行时毫无用处。

# ---------- 阶段一：构建 ----------
FROM node:22-alpine AS builder

WORKDIR /build

# 先只拷依赖清单：只要依赖没变，这一层就能命中缓存，
# 不必每次改源码都重装一遍 node_modules。
#
# 用 `npm ci` 而不是 `npm install`：`ci` 严格按 package-lock.json 装，
# 装出来的版本与本地/CI 完全一致；`install` 会顺手升级 lock 里允许的范围，
# 于是"本地测过"和"镜像里跑的"可能是两个版本。
# 也正因如此，lock 文件必须存在且被显式拷进来 —— 原来的 `package-lock.json*`
# 通配符在文件缺失时会静默跳过，`npm ci` 直接报错，反而是我们想要的失败方式。
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build


# ---------- 阶段二：运行 ----------
FROM nginx:1.27-alpine

# 移除默认站点配置，用我们自己的
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/flawscope.conf

# 拷入构建产物
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD wget -qO- http://localhost/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
