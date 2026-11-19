# WhatsApp transport bridge (Baileys). Transport only: no business logic,
# no database, no broker. Talks to the app over the compose network.
FROM node:26-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY src/ src/

# runtime data (auth session, durable queues) lives on the bind mount
RUN mkdir -p /data/whatsapp-bridge && chown -R node:node /data/whatsapp-bridge /app
USER node

ENV NODE_ENV=production \
    WHATSAPP_BRIDGE_DATA_DIR=/data/whatsapp-bridge \
    WHATSAPP_BRIDGE_HOST=0.0.0.0 \
    WHATSAPP_BRIDGE_PORT=3001

EXPOSE 3001
# never published: reachable only inside the compose network

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3001/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "src/index.js"]
