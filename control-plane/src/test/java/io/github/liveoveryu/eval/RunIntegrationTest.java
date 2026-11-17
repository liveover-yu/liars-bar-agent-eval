package io.github.liveoveryu.eval;

import static io.github.liveoveryu.eval.Protocol.*;
import static org.assertj.core.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import java.time.*;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.IntFunction;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.web.servlet.MockMvc;
import com.fasterxml.jackson.databind.ObjectMapper;

@SpringBootTest(properties={
    "spring.datasource.url=jdbc:h2:mem:run_tests;MODE=PostgreSQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1;LOCK_TIMEOUT=10000",
    "eval.api-key=test-key", "eval.lease-seconds=30"
})
@AutoConfigureMockMvc
class RunIntegrationTest {
    @Autowired RunService runs;
    @Autowired JdbcTemplate db;
    @Autowired MutableClock clock;
    @Autowired MockMvc http;
    @Autowired ObjectMapper json;

    @TestConfiguration static class TimeConfig {
        @Bean @Primary MutableClock testClock() { return new MutableClock(); }
    }
    static class MutableClock extends Clock {
        private final AtomicLong now = new AtomicLong(1_800_000_000_000L);
        void advance(long millis) { now.addAndGet(millis); }
        void reset() { now.set(1_800_000_000_000L); }
        public ZoneId getZone() { return ZoneOffset.UTC; }
        public Clock withZone(ZoneId zone) { return this; }
        public Instant instant() { return Instant.ofEpochMilli(now.get()); }
        public long millis() { return now.get(); }
    }
    @BeforeEach void clean() {
        db.update("DELETE FROM eval_attempt"); db.update("DELETE FROM eval_case"); db.update("DELETE FROM eval_run"); clock.reset();
    }
    private CreateRequest config(int maxAttempts, int maxCalls) {
        return new CreateRequest("mock",null,List.of("QQJ_3"),null,1,0L,0.0,maxAttempts,maxCalls,null,null);
    }
    private RunView create(int maxAttempts, int maxCalls) { return runs.create(UUID.randomUUID().toString(), config(maxAttempts,maxCalls)).run(); }
    private FinishRequest success(String token) {
        return new FinishRequest(token,"SUCCESS",false,true,true,null,null,25L,10L,2L,"a".repeat(64),"mock-policy",SUITE,"{\"was_challenged\":true}");
    }
    private String reserve(Claim claim,String caseId) {
        String attempt = UUID.randomUUID().toString(); runs.reserve(claim.runId(),new AttemptRequest(claim.leaseToken(),caseId,attempt)); return attempt;
    }
    private void errorCode(String code, org.assertj.core.api.ThrowableAssert.ThrowingCallable action) {
        assertThatThrownBy(action).isInstanceOfSatisfying(ApiException.class,e -> assertThat(e.code()).isEqualTo(code));
    }
    private <T> List<T> concurrently(int n,IntFunction<T> operation) throws Exception {
        try (ExecutorService pool = Executors.newFixedThreadPool(n)) {
            CountDownLatch start = new CountDownLatch(1);
            List<Future<T>> futures = new ArrayList<>();
            for(int i=0;i<n;i++) { int index=i; futures.add(pool.submit(() -> { start.await(); return operation.apply(index); })); }
            start.countDown(); List<T> results = new ArrayList<>();
            for(Future<T> f:futures) results.add(f.get(15,TimeUnit.SECONDS));
            return results;
        }
    }

    @Test void normalizesDefaultsAndRejectsIdempotencyConflicts() {
        CreateResult first=runs.create("same",null);
        CreateResult replay=runs.create("same",new CreateRequest("mock","mock-policy",BOARDS,"neutral",1,0L,0.0,3,144,30,512));
        assertThat(first.created()).isTrue(); assertThat(replay.created()).isFalse();
        assertThat(replay.run().runId()).isEqualTo(first.run().runId());
        assertThat(first.run().totalCases()).isEqualTo(48);
        errorCode("IDEMPOTENCY_CONFLICT",() -> runs.create("same",config(2,12)));
    }
    @Test void concurrentCreateUsesOneRunAndOneCaseSet() throws Exception {
        List<CreateResult> results=concurrently(8,i -> runs.create("one-key",config(3,18)));
        assertThat(results.stream().filter(CreateResult::created).count()).isEqualTo(1);
        assertThat(results.stream().map(r -> r.run().runId()).distinct().count()).isEqualTo(1);
        assertThat(db.queryForObject("SELECT COUNT(*) FROM eval_case",Integer.class)).isEqualTo(6);
    }
    @Test void concurrentWorkersCannotClaimTheSameLiveRun() throws Exception {
        RunView run=create(3,18);
        List<Claim> results=concurrently(8,i -> runs.claim("worker-"+i));
        assertThat(results.stream().filter(Objects::nonNull).count()).isEqualTo(1);
        assertThat(runs.get(run.runId()).status()).isEqualTo("RUNNING");
    }
    @Test void parallelWorkersClaimDistinctRuns() throws Exception {
        create(3,18); create(3,18); create(3,18);
        List<Claim> results=concurrently(6,i -> runs.claim("w-"+i));
        assertThat(results.stream().filter(Objects::nonNull).map(Claim::runId).distinct().count()).isEqualTo(3);
    }
    @Test void expiredLeaseIsFencedAndOldAttemptsCannotBeFinishedByNewOwner() {
        create(3,18); Claim old=runs.claim("old"); String c=old.cases().getFirst().caseId(); String a=reserve(old,c);
        clock.advance(30_001);
        errorCode("LEASE_LOST",() -> runs.heartbeat(old.runId(),old.leaseToken()));
        Claim next=runs.claim("new"); assertThat(next.leaseToken()).isNotEqualTo(old.leaseToken());
        errorCode("LEASE_LOST",() -> runs.finish(old.runId(),a,success(old.leaseToken())));
        errorCode("LEASE_LOST",() -> runs.finish(next.runId(),a,success(next.leaseToken())));
        String replacement=reserve(next,c);
        runs.finish(next.runId(),replacement,success(next.leaseToken()));
        assertThat(runs.get(next.runId()).callCount()).isEqualTo(2);
        assertThat(runs.get(next.runId()).completedCases()).isEqualTo(1);
        assertThat(runs.report(next.runId()).get("unfinishedAttempts")).isEqualTo(1L);
    }
    @Test void cancelResumePreservesCheckpointAndBudgetAndChangesToken() {
        create(3,18); Claim before=runs.claim("one"); String c=before.cases().getFirst().caseId();
        String a=reserve(before,c); runs.finish(before.runId(),a,success(before.leaseToken()));
        runs.cancel(before.runId());
        errorCode("LEASE_LOST",() -> runs.heartbeat(before.runId(),before.leaseToken()));
        assertThat(runs.resume(before.runId()).status()).isEqualTo("QUEUED");
        Claim after=runs.claim("two");
        assertThat(after.completedCaseIds()).containsExactly(c); assertThat(after.leaseToken()).isNotEqualTo(before.leaseToken());
        assertThat(runs.get(after.runId()).callCount()).isEqualTo(1);
        errorCode("CASE_COMPLETED",() -> reserve(after,c));
        assertThat(runs.results(after.runId(),0,100).total()).isEqualTo(1);
    }
    @Test void repeatedReserveAndFinishDoNotChargeTwiceAndDifferentFinishConflicts() {
        create(3,18); Claim claim=runs.claim("w"); String c=claim.cases().getFirst().caseId(); String a=reserve(claim,c);
        AttemptResult replay=runs.reserve(claim.runId(),new AttemptRequest(claim.leaseToken(),c,a));
        assertThat(replay.created()).isFalse(); assertThat(replay.attempt().totalCalls()).isEqualTo(1);
        runs.finish(claim.runId(),a,success(claim.leaseToken())); runs.finish(claim.runId(),a,success(claim.leaseToken()));
        assertThat(runs.report(claim.runId()).get("totalTokens")).isEqualTo(12L);
        assertThat(runs.get(claim.runId()).completedCases()).isEqualTo(1);
        FinishRequest changed=new FinishRequest(claim.leaseToken(),"SUCCESS",false,false,true,null,null,25L,10L,2L,"a".repeat(64),"mock-policy",SUITE,"changed");
        errorCode("ATTEMPT_CONFLICT",() -> runs.finish(claim.runId(),a,changed));
    }
    @Test void globalBudgetIsAtomicUnderConcurrentReservations() throws Exception {
        create(3,1); Claim claim=runs.claim("w");
        List<String> outcomes=concurrently(6,i -> {
            try { reserve(claim,claim.cases().get(i).caseId()); return "OK"; }
            catch(ApiException e) { return e.code(); }
        });
        assertThat(outcomes.stream().filter("OK"::equals).count()).isEqualTo(1);
        assertThat(outcomes.stream().filter("BUDGET_EXHAUSTED"::equals).count()).isEqualTo(5);
        runs.fail(claim.runId(),new FailRequest(claim.leaseToken(),"BUDGET_EXHAUSTED","已耗尽"));
        errorCode("BUDGET_EXHAUSTED",() -> runs.resume(claim.runId()));
        assertThat(runs.get(claim.runId()).callCount()).isEqualTo(1);
    }
    @Test void startedAttemptStillConsumesPerCaseBudgetAfterLeaseExpires() {
        create(1,6); Claim claim=runs.claim("old"); String c=claim.cases().getFirst().caseId(); reserve(claim,c);
        clock.advance(30_001); Claim next=runs.claim("new");
        errorCode("ATTEMPTS_EXHAUSTED",() -> reserve(next,c));
        assertThat(runs.get(next.runId()).completedCases()).isZero();
    }
    @Test void retryableErrorsRemainPendingUntilSuccessAndAreCountedInReport() {
        create(3,18); Claim claim=runs.claim("w"); String c=claim.cases().getFirst().caseId(); String a=reserve(claim,c);
        FinishRequest error=new FinishRequest(claim.leaseToken(),"ERROR",true,null,null,"FORMAT","bad json",100L,10L,5L,"b".repeat(64),"mock-policy",SUITE,"bad");
        runs.finish(claim.runId(),a,error); assertThat(runs.get(claim.runId()).completedCases()).isZero();
        String second=reserve(claim,c); runs.finish(claim.runId(),second,success(claim.leaseToken()));
        Map<String,Object> report=runs.report(claim.runId());
        assertThat(report.get("totalTokens")).isEqualTo(27L); assertThat(report.get("errorCounts")).isEqualTo(Map.of("FORMAT",1L));
        assertThat(report.get("p95LatencyMs")).isEqualTo(100L); assertThat(report.get("agreementWithReference")).isEqualTo(1.0);
    }
    @Test void nonRetryableErrorCheckpointsAndCompleteUsesFailedStatus() {
        create(3,18); Claim claim=runs.claim("w");
        for(CaseView c:claim.cases()) {
            String a=reserve(claim,c.caseId());
            if(c.equals(claim.cases().getFirst())) runs.finish(claim.runId(),a,new FinishRequest(claim.leaseToken(),"ERROR",false,null,null,"AUTH","bad key",10L,0L,0L,"a".repeat(64),"mock",SUITE,""));
            else runs.finish(claim.runId(),a,success(claim.leaseToken()));
        }
        assertThat(runs.complete(claim.runId(),claim.leaseToken()).status()).isEqualTo("FAILED");
        assertThat(runs.get(claim.runId()).failureCases()).isEqualTo(1);
        errorCode("NO_UNFINISHED_CASES",() -> runs.resume(claim.runId()));
    }
    @Test void completeRequiresAllCheckpointsAndInvalidatesLease() {
        create(3,18); Claim claim=runs.claim("w");
        errorCode("CASES_INCOMPLETE",() -> runs.complete(claim.runId(),claim.leaseToken()));
        for(CaseView c:claim.cases()) { String a=reserve(claim,c.caseId()); runs.finish(claim.runId(),a,success(claim.leaseToken())); }
        assertThat(runs.complete(claim.runId(),claim.leaseToken()).status()).isEqualTo("SUCCEEDED");
        errorCode("LEASE_LOST",() -> runs.heartbeat(claim.runId(),claim.leaseToken()));
        assertThat(runs.report(claim.runId()).get("challengeRate")).isEqualTo(1.0);
    }
    @Test void emptyReportUsesNullAndHttpAuthenticationAndValidationAreEnforced() throws Exception {
        RunView created=create(3,18); assertThat(runs.report(created.runId()).get("challengeRate")).isNull();
        http.perform(get("/actuator/health")).andExpect(status().isOk());
        http.perform(get("/api/runs")).andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("UNAUTHORIZED"));
        http.perform(post("/api/runs").header("X-API-Key","test-key").contentType("application/json").content("{}"))
            .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_REQUEST"));
        http.perform(post("/api/runs").header("X-API-Key","test-key").header("Idempotency-Key","invalid").contentType("application/json").content("{\"boards\":[null]}"))
            .andExpect(status().isBadRequest());
        http.perform(get("/api/runs?limit=abc").header("X-API-Key","test-key")).andExpect(status().isBadRequest());
    }
    @Test void httpCreateReturnsCorrectStatusesAndUnknownFieldsAreRejected() throws Exception {
        http.perform(post("/api/runs").header("X-API-Key","test-key").header("Idempotency-Key","http-key").contentType("application/json").content("{}"))
            .andExpect(status().isCreated()).andExpect(jsonPath("$.status").value("QUEUED"));
        http.perform(post("/api/runs").header("X-API-Key","test-key").header("Idempotency-Key","http-key").contentType("application/json").content("{}"))
            .andExpect(status().isOk());
        http.perform(post("/api/runs").header("X-API-Key","test-key").header("Idempotency-Key","http-key").contentType("application/json").content("{\"repeats\":2}"))
            .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("IDEMPOTENCY_CONFLICT"));
        http.perform(post("/api/runs").header("X-API-Key","test-key").header("Idempotency-Key","other").contentType("application/json").content("{\"typo\":true}"))
            .andExpect(status().isBadRequest());
    }
    @Test void javaSuiteConstantsAndCaseExpansionMatchSharedContract() throws Exception {
        Path contract=Path.of("contracts/probe-suite-v2.json");
        if(!Files.exists(contract)) contract=Path.of("../contracts/probe-suite-v2.json");
        var suite=json.readTree(Files.readString(contract));
        assertThat(suite.path("suiteVersion").asText()).isEqualTo(SUITE);
        List<String> boards=new ArrayList<>(); suite.path("boards").forEach(n -> boards.add(n.asText()));
        assertThat(boards).isEqualTo(BOARDS);
        runs.create("ordered",new CreateRequest(null,null,List.of("QQJ_3","noQ_1"),null,2,123L,null,null,null,null,null));
        Claim claim=runs.claim("w");
        assertThat(claim.cases().getFirst()).isEqualTo(new CaseView("QQJ_3_gun0:0","QQJ_3_gun0",0,123));
        assertThat(claim.cases().get(1)).isEqualTo(new CaseView("QQJ_3_gun0:1","QQJ_3_gun0",1,124));
        assertThat(claim.cases().get(12)).isEqualTo(new CaseView("noQ_1_gun0:0","noQ_1_gun0",0,135));
    }
    @Test void invalidDecisionPayloadCannotChangeMetricsOrCheckpoint() {
        create(3,18); Claim claim=runs.claim("w"); String c=claim.cases().getFirst().caseId(); String a=reserve(claim,c);
        FinishRequest missingReference=new FinishRequest(claim.leaseToken(),"SUCCESS",false,true,null,null,null,1L,0L,0L,"a".repeat(64),"mock",SUITE,"");
        errorCode("INVALID_REQUEST",() -> runs.finish(claim.runId(),a,missingReference));
        FinishRequest errorWithDecision=new FinishRequest(claim.leaseToken(),"ERROR",false,true,null,"FORMAT","bad",1L,0L,0L,"a".repeat(64),"mock",SUITE,"");
        errorCode("INVALID_REQUEST",() -> runs.finish(claim.runId(),a,errorWithDecision));
        assertThat(runs.get(claim.runId()).completedCases()).isZero();
        assertThat(runs.report(claim.runId()).get("challengeRate")).isNull();
        FinishRequest wrongHash=new FinishRequest(claim.leaseToken(),"SUCCESS",false,true,true,null,null,1L,0L,0L,"a".repeat(64),"mock",SUITE,"","prompt","req-1","a".repeat(64));
        errorCode("INVALID_REQUEST",() -> runs.finish(claim.runId(),a,wrongHash));
        FinishRequest withProvenance=new FinishRequest(claim.leaseToken(),"SUCCESS",false,true,true,null,null,1L,0L,0L,
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","mock",SUITE,"","","req-1","a".repeat(64));
        runs.finish(claim.runId(),a,withProvenance);
        assertThat(runs.results(claim.runId(),0,10).items().getFirst()).containsEntry("prompt","")
            .containsEntry("requestId","req-1").containsEntry("workerCodeHash","a".repeat(64));
    }
    @Test void resumeRefusesUnrecoverableCaseAttemptBudget() {
        create(1,6); Claim claim=runs.claim("w"); reserve(claim,claim.cases().getFirst().caseId());
        runs.cancel(claim.runId());
        errorCode("ATTEMPTS_EXHAUSTED",() -> runs.resume(claim.runId()));
        assertThat(runs.get(claim.runId()).status()).isEqualTo("CANCELLED");
    }
}
