ARG VITE_DEPS_IMAGE
FROM ${VITE_DEPS_IMAGE}
WORKDIR /app
RUN ln -s /opt/real-node-deps/node_modules /app/node_modules
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev"]
