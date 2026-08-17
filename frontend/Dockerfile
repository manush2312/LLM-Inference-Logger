# Frontend image: build the bundle, then serve it as static files from nginx.
#
# nginx also proxies the API paths, so the browser talks to a single origin in
# production exactly as it does behind the Kubernetes ingress -- CORS is a
# development-only concern rather than a production dependency.

# --- Builder -----------------------------------------------------------------
FROM node:22-alpine AS builder

WORKDIR /app

# Lockfile-only layer, so a source change does not reinstall node_modules.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Baked at build time: Vite inlines env vars into the bundle, so this cannot be
# set at container start. Empty means "same origin", which is what the nginx
# proxy below provides.
ENV VITE_API_BASE_URL=""
RUN npm run build

# --- Runtime -----------------------------------------------------------------
# The unprivileged variant, not plain `nginx`. The stock image's entrypoint
# chowns its cache directory at start, which needs CAP_CHOWN -- and the pod's
# security context drops all capabilities. This image runs as UID 101, listens
# on 8080, and needs neither. Loosening the security context to suit the base
# image would be the wrong way round.
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

# Opts the entrypoint into populating NGINX_LOCAL_RESOLVERS from
# /etc/resolv.conf. The script that does it returns early unless this is
# set, which leaves the placeholder unsubstituted and nginx refusing to
# start with `host not found in resolver "${NGINX_LOCAL_RESOLVERS}"`.
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1

COPY --from=builder /app/dist /usr/share/nginx/html
# A template, not a finished conf: the image's entrypoint runs envsubst over
# /etc/nginx/templates/*.template, which is how ${NGINX_LOCAL_RESOLVERS} gets
# filled in from the container's own /etc/resolv.conf.
COPY templates/default.conf.template /etc/nginx/templates/default.conf.template

# Unprivileged nginx binds an unprivileged port; 80 would need
# CAP_NET_BIND_SERVICE.
EXPOSE 8080
