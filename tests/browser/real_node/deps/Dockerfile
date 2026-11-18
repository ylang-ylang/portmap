FROM node:22.22.3-alpine
WORKDIR /opt/real-node-deps
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund && npm cache clean --force
