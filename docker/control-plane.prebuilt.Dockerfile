# Build the jar on the host first: mvn -f control-plane/pom.xml verify
ARG JAVA_RUNTIME_IMAGE=eclipse-temurin:21-jre
FROM ${JAVA_RUNTIME_IMAGE}
WORKDIR /app
COPY control-plane/target/liars-eval-control-plane-0.1.0.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
