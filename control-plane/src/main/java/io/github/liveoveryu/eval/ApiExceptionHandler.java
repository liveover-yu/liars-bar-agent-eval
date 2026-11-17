package io.github.liveoveryu.eval;

import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.web.servlet.resource.NoResourceFoundException;
import org.springframework.dao.DataIntegrityViolationException;

@RestControllerAdvice
class ApiExceptionHandler {
    @ExceptionHandler(ApiException.class)
    ResponseEntity<?> domainError(ApiException e) {
        return ResponseEntity.status(e.status()).body(Map.of("code", e.code(), "message", e.getMessage()));
    }
    @ExceptionHandler({MethodArgumentNotValidException.class, HttpMessageNotReadableException.class, MethodArgumentTypeMismatchException.class})
    ResponseEntity<?> validation(Exception e) {
        return ResponseEntity.badRequest().body(Map.of("code", "INVALID_REQUEST", "message", "请求字段不符合接口约束"));
    }
    @ExceptionHandler(NoResourceFoundException.class)
    ResponseEntity<?> notFound(NoResourceFoundException e) {
        return ResponseEntity.status(404).body(Map.of("code","NOT_FOUND","message","接口不存在"));
    }
    @ExceptionHandler(DataIntegrityViolationException.class)
    ResponseEntity<?> constraintConflict(DataIntegrityViolationException e) {
        return ResponseEntity.status(409).body(Map.of("code","DATA_CONFLICT","message","数据唯一性冲突，请使用原请求重放或新的UUID"));
    }
}
