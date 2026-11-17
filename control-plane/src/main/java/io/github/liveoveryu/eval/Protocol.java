package io.github.liveoveryu.eval;

import jakarta.validation.constraints.*;
import java.time.Instant;
import java.util.List;
import java.util.Map;

public final class Protocol {
    private Protocol() {}
    public static final String SUITE = "probe-v2";
    public static final List<String> BOARDS = List.of("noQ_1", "noQ_2", "noQ_3", "noQ_A2", "noQ_A3", "AJJ_2", "QQQ_2", "QQJ_3");
    public record CreateRequest(String mode, String model, List<String> boards, String ruleBase,
        Integer repeats, Long seed, Double temperature, Integer maxAttempts, Integer maxCalls,
        Integer timeoutSeconds, Integer maxOutputTokens) {}
    public record Config(String mode, String model, List<String> boards, String ruleBase,
        int repeats, long seed, double temperature, int maxAttempts, int maxCalls,
        int timeoutSeconds, int maxOutputTokens) {}
    public record RunView(String runId, String status, Config config, String suiteVersion,
        int totalCases, int completedCases, int successCases, int failureCases, int callCount,
        Instant createdAt, Instant updatedAt, String lastError) {}
    public record CreateResult(RunView run, boolean created) {}
    public record ClaimRequest(@NotBlank @Size(max=128) String workerId) {}
    public record LeaseRequest(@NotBlank String leaseToken) {}
    public record CaseView(String caseId, String probeId, int repeatIndex, long seed) {}
    public record Claim(String runId, String leaseToken, int leaseSeconds, Instant leaseExpiresAt,
        Config config, String suiteVersion, List<CaseView> cases, List<String> completedCaseIds) {}
    public record LeaseView(Instant leaseExpiresAt, int leaseSeconds) {}
    public record AttemptRequest(@NotBlank String leaseToken, @NotBlank String caseId, @NotBlank String attemptId) {}
    public record AttemptView(String attemptId, int attemptNumber, int totalCalls) {}
    public record AttemptResult(AttemptView attempt, boolean created) {}
    public record FinishRequest(@NotBlank String leaseToken, @NotBlank String outcome,
        @NotNull Boolean retryable, Boolean wasChallenged, Boolean referenceDecision,
        String errorType, @Size(max=4000) String errorMessage,
        @NotNull @PositiveOrZero Long latencyMs, @NotNull @PositiveOrZero Long promptTokens,
        @NotNull @PositiveOrZero Long completionTokens,
        @NotBlank @Pattern(regexp="[a-fA-F0-9]{64}") String promptHash,
        @NotBlank @Size(max=200) String modelVersion, @NotBlank String suiteVersion,
        @NotNull @Size(max=200000) String content, @Size(max=100000) String prompt,
        @Size(max=200) String requestId, @Pattern(regexp="[a-fA-F0-9]{64}") String workerCodeHash) {
        public FinishRequest(String leaseToken, String outcome, Boolean retryable, Boolean wasChallenged,
            Boolean referenceDecision, String errorType, String errorMessage, Long latencyMs,
            Long promptTokens, Long completionTokens, String promptHash, String modelVersion,
            String suiteVersion, String content) {
            this(leaseToken,outcome,retryable,wasChallenged,referenceDecision,errorType,errorMessage,
                latencyMs,promptTokens,completionTokens,promptHash,modelVersion,suiteVersion,content,null,null,null);
        }
    }
    public record FailRequest(@NotBlank String leaseToken, @NotBlank @Size(max=80) String code,
        @NotBlank @Size(max=4000) String message) {}
    public record Page(List<Map<String,Object>> items, int total, int page, int size) {}
}
