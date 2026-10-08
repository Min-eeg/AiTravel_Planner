# ===== 阶段一：构建静态资源 =====
FROM node:22-alpine AS build

ARG VITE_AMAP_WEB_KEY
ENV VITE_AMAP_WEB_KEY=${VITE_AMAP_WEB_KEY}

WORKDIR /app

# 先拷锁文件装依赖：锁文件不变时利用缓存层，改代码不重装
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ===== 阶段二：nginx 托管 + 反代后端 =====
# 构建产物只有静态文件，最终镜像不含 node_modules 和源码
FROM nginx:alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
