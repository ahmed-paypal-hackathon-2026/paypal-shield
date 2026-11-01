# DevOps-owned image for the React storefront (Person 3's source in frontend/src).
# Multi-stage: build with Vite, serve the static bundle on nginx with SPA fallback.
FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
ARG VITE_API_BASE_URL=http://localhost:8000
ARG VITE_PAYPAL_CLIENT_ID=
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_PAYPAL_CLIENT_ID=$VITE_PAYPAL_CLIENT_ID
RUN npm run build

FROM nginx:1.27-alpine AS runtime

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 5173

HEALTHCHECK --interval=15s --timeout=3s \
  CMD wget -qO- http://localhost:5173/ >/dev/null 2>&1 || exit 1
