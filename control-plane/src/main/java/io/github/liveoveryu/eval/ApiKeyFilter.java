package io.github.liveoveryu.eval;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

@Component
class ApiKeyFilter extends OncePerRequestFilter {
    private final byte[] expected;
    private final ObjectMapper json;
    ApiKeyFilter(@Value("${eval.api-key}") String key, ObjectMapper json) {
        if (key.isBlank()) throw new IllegalArgumentException("eval.api-key must not be blank");
        this.expected = key.getBytes(StandardCharsets.UTF_8);
        this.json = json;
    }
    @Override protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
        FilterChain chain) throws ServletException, IOException {
        if (request.getRequestURI().startsWith("/api/")) {
            String supplied = request.getHeader("X-API-Key");
            if (supplied == null || !MessageDigest.isEqual(expected, supplied.getBytes(StandardCharsets.UTF_8))) {
                response.setStatus(401);
                response.setContentType("application/json");
                json.writeValue(response.getOutputStream(), Map.of("code", "UNAUTHORIZED", "message", "X-API-Key 无效"));
                return;
            }
        }
        chain.doFilter(request, response);
    }
}
