package io.github.liveoveryu.eval;

/** Stable machine-readable errors for both clients and workers. */
public class ApiException extends RuntimeException {
    private final int status;
    private final String code;
    public ApiException(int status, String code, String message) {
        super(message);
        this.status = status;
        this.code = code;
    }
    public int status() { return status; }
    public String code() { return code; }
}
