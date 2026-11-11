# El panel, para Railway.
#
# Existe por una razón concreta: el `railway.json` de la raíz fija el constructor en
# DOCKERFILE para todos los servicios del repo, y la ruta del Dockerfile se resuelve
# dentro de la carpeta raíz de cada servicio. La del panel es `panel/`, así que sin este
# archivo Railway intentaba construirlo con el Dockerfile del worker de Python y fallaba
# buscando `uv.lock`.
FROM node:22-slim AS build
WORKDIR /app
# Las dependencias primero: cambian mucho menos que el código y así la capa se reutiliza.
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
# Las NEXT_PUBLIC_* se incrustan en el bundle del navegador, así que tienen que estar
# presentes AL CONSTRUIR y no solo al arrancar. Railway pasa las variables del servicio
# a los dos momentos.
ARG NEXT_PUBLIC_SUPABASE_URL
ARG NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY
ENV NEXT_PUBLIC_SUPABASE_URL=$NEXT_PUBLIC_SUPABASE_URL
ENV NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=$NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production
# Sin root, como el worker.
RUN useradd --create-home --uid 10001 panel
COPY --from=build --chown=panel:panel /app /app
USER panel
# `npm start` ya fija -H 0.0.0.0; el puerto lo pone Railway en PORT.
CMD ["npm", "start"]
