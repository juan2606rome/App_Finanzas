# ruta: microservicio-java/Dockerfile
# Etapa 1: compilar con el JDK
FROM eclipse-temurin:21-jdk AS build
WORKDIR /app
COPY Main.java .
RUN javac -encoding UTF-8 -d out Main.java

# Etapa 2: imagen liviana solo con el JRE para ejecutar
FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /app/out ./out
# Documentación Swagger (se sirve en /docs y /swagger.json)
COPY swagger.json .
# Render (plan gratis) da 512 MB de RAM: limitamos el heap de Java.
ENV JAVA_TOOL_OPTIONS="-Xmx256m"
EXPOSE 8080
CMD ["java", "-cp", "out", "Main"]