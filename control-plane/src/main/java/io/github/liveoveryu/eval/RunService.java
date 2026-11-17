package io.github.liveoveryu.eval;

import static io.github.liveoveryu.eval.Protocol.*;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Instant;
import java.util.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.annotation.Isolation;

/** The database, rather than an individual JVM, owns leases, budgets and checkpoints. */
@Service
public class RunService {
    private final JdbcTemplate db;
    private final ObjectMapper json;
    private final Clock clock;
    private final int leaseSeconds;
    public RunService(JdbcTemplate db, ObjectMapper json, Clock clock,
        @Value("${eval.lease-seconds:30}") int leaseSeconds) {
        if (leaseSeconds < 1 || leaseSeconds > 3600) throw new IllegalArgumentException("invalid lease seconds");
        this.db = db; this.json = json; this.clock = clock; this.leaseSeconds = leaseSeconds;
    }

    @Transactional
    public CreateResult create(String key, CreateRequest body) {
        if (key == null || key.isBlank() || key.length() > 200) bad("Idempotency-Key 必填，长度不超过200");
        Config config = normalize(body);
        String payload = encode(config), hash = hash(payload);
        // One short coordinator lock makes concurrent create/claim portable across H2 and PostgreSQL.
        coordinatorLock();
        List<Map<String,Object>> existing = db.queryForList("SELECT * FROM eval_run WHERE idempotency_key=?", key);
        if (!existing.isEmpty()) {
            Map<String,Object> run = existing.getFirst();
            if (!hash.equals(run.get("config_hash"))) conflict("IDEMPOTENCY_CONFLICT", "该幂等键已用于不同配置");
            return new CreateResult(view(run), false);
        }
        String id = UUID.randomUUID().toString(); long now = clock.millis();
        int total = config.boards().size() * 6 * config.repeats();
        db.update("INSERT INTO eval_run (run_id,idempotency_key,config_hash,config_json,status,suite_version,total_cases,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            id, key, hash, payload, "QUEUED", SUITE, total, now, now);
        int ordinal = 0;
        for (String board : config.boards()) for (int gun = 0; gun < 6; gun++) for (int repeat = 0; repeat < config.repeats(); repeat++) {
            String probe = board + "_gun" + gun;
            db.update("INSERT INTO eval_case (run_id,case_id,probe_id,repeat_index,case_seed,ordinal) VALUES (?,?,?,?,?,?)",
                id, probe + ":" + repeat, probe, repeat, config.seed() + ordinal, ordinal);
            ordinal++;
        }
        return new CreateResult(view(run(id, false)), true);
    }

    @Transactional(readOnly=true,isolation=Isolation.REPEATABLE_READ)
    public RunView get(String id) { return view(run(id, false)); }
    @Transactional(readOnly=true,isolation=Isolation.REPEATABLE_READ)
    public List<RunView> list(int limit) {
        if (limit < 1 || limit > 100) bad("limit 必须介于1和100");
        return db.queryForList("SELECT * FROM eval_run ORDER BY created_at DESC,run_id DESC LIMIT ?", limit).stream().map(this::view).toList();
    }

    @Transactional
    public Claim claim(String workerId) {
        coordinatorLock();
        long now = clock.millis();
        List<Map<String,Object>> candidates = db.queryForList("SELECT * FROM eval_run WHERE status='QUEUED' OR (status='RUNNING' AND lease_until<=?) ORDER BY created_at,run_id LIMIT 1 FOR UPDATE", now);
        if (candidates.isEmpty()) return null;
        Map<String,Object> run = candidates.getFirst(); String id = str(run,"run_id");
        String token = UUID.randomUUID().toString(); long until = now + leaseSeconds * 1000L;
        db.update("UPDATE eval_run SET status='RUNNING',lease_token=?,worker_id=?,lease_until=?,updated_at=? WHERE run_id=?",
            token, workerId, until, now, id);
        List<Map<String,Object>> cases = db.queryForList("SELECT * FROM eval_case WHERE run_id=? ORDER BY ordinal", id);
        return new Claim(id, token, leaseSeconds, Instant.ofEpochMilli(until), config(run), SUITE,
            cases.stream().map(c -> new CaseView(str(c,"case_id"), str(c,"probe_id"), number(c,"repeat_index"), longNumber(c,"case_seed"))).toList(),
            cases.stream().filter(c -> !"PENDING".equals(c.get("status"))).map(c -> str(c,"case_id")).toList());
    }

    @Transactional
    public LeaseView heartbeat(String id, String token) {
        Map<String,Object> run = run(id, true); requireLease(run, token);
        long now = clock.millis(), until = now + leaseSeconds * 1000L;
        db.update("UPDATE eval_run SET lease_until=?,updated_at=? WHERE run_id=?", until, now, id);
        return new LeaseView(Instant.ofEpochMilli(until), leaseSeconds);
    }

    @Transactional
    public AttemptResult reserve(String id, AttemptRequest request) {
        Map<String,Object> run = run(id, true); requireLease(run, request.leaseToken());
        try { UUID.fromString(request.attemptId()); } catch (IllegalArgumentException e) { bad("attemptId 必须是UUID"); }
        List<Map<String,Object>> prior = db.queryForList("SELECT * FROM eval_attempt WHERE attempt_id=?", request.attemptId());
        if (!prior.isEmpty()) {
            Map<String,Object> a = prior.getFirst();
            if (!id.equals(a.get("run_id")) || !request.caseId().equals(a.get("case_id")) || !request.leaseToken().equals(a.get("lease_token")))
                conflict("ATTEMPT_CONFLICT", "attemptId 已被其他case或lease使用");
            return new AttemptResult(new AttemptView(request.attemptId(), number(a,"attempt_number"), number(run,"call_count")), false);
        }
        Map<String,Object> c = caseRow(id, request.caseId());
        if (!"PENDING".equals(c.get("status"))) conflict("CASE_COMPLETED", "该case已有checkpoint");
        Config config = config(run);
        if (number(c,"attempt_count") >= config.maxAttempts()) conflict("ATTEMPTS_EXHAUSTED", "该case的attempt预算已耗尽");
        if (number(run,"call_count") >= config.maxCalls()) conflict("BUDGET_EXHAUSTED", "该run的调用预算已耗尽");
        int attemptNumber = number(c,"attempt_count") + 1, calls = number(run,"call_count") + 1;
        long now = clock.millis();
        db.update("INSERT INTO eval_attempt (attempt_id,run_id,case_id,lease_token,attempt_number,created_at) VALUES (?,?,?,?,?,?)",
            request.attemptId(), id, request.caseId(), request.leaseToken(), attemptNumber, now);
        db.update("UPDATE eval_case SET attempt_count=? WHERE run_id=? AND case_id=?", attemptNumber, id, request.caseId());
        db.update("UPDATE eval_run SET call_count=?,updated_at=? WHERE run_id=?", calls, now, id);
        return new AttemptResult(new AttemptView(request.attemptId(), attemptNumber, calls), true);
    }

    @Transactional
    public Map<String,Object> finish(String id, String attemptId, FinishRequest request) {
        Map<String,Object> run = run(id, true); requireLease(run, request.leaseToken());
        validateFinish(request);
        List<Map<String,Object>> rows = db.queryForList("SELECT * FROM eval_attempt WHERE run_id=? AND attempt_id=?", id, attemptId);
        if (rows.isEmpty()) throw new ApiException(404, "NOT_FOUND", "attempt不存在");
        Map<String,Object> attempt = rows.getFirst();
        if (!request.leaseToken().equals(attempt.get("lease_token"))) conflict("LEASE_LOST", "attempt属于旧租约");
        String payload = encode(request), finishHash = hash(payload);
        if ("FINISHED".equals(attempt.get("status"))) {
            if (!finishHash.equals(attempt.get("finish_hash"))) conflict("ATTEMPT_CONFLICT", "已完成attempt的结果不能修改");
            return finishView(id, attemptId, str(attempt,"case_id"));
        }
        Map<String,Object> c = caseRow(id, str(attempt,"case_id"));
        if (!"PENDING".equals(c.get("status"))) conflict("CASE_COMPLETED", "该case已有checkpoint");
        long now = clock.millis();
        db.update("UPDATE eval_attempt SET status='FINISHED',outcome=?,error_type=?,latency_ms=?,prompt_tokens=?,completion_tokens=?,was_challenged=?,reference_decision=?,finish_hash=?,finish_json=?,finished_at=? WHERE attempt_id=?",
            request.outcome(), request.errorType(), request.latencyMs(), request.promptTokens(), request.completionTokens(),
            request.wasChallenged(), request.referenceDecision(), finishHash, payload, now, attemptId);
        Config config = config(run);
        boolean checkpoint = "SUCCESS".equals(request.outcome()) || !request.retryable()
            || number(c,"attempt_count") >= config.maxAttempts() || number(run,"call_count") >= config.maxCalls();
        if (checkpoint) db.update("UPDATE eval_case SET status=?,finished_attempt_id=? WHERE run_id=? AND case_id=?",
            request.outcome(), attemptId, id, c.get("case_id"));
        db.update("UPDATE eval_run SET updated_at=?,last_error=? WHERE run_id=?", now,
            "ERROR".equals(request.outcome()) ? request.errorType() + ": " + Objects.toString(request.errorMessage(), "") : run.get("last_error"), id);
        return finishView(id, attemptId, str(c,"case_id"));
    }

    @Transactional
    public RunView complete(String id, String token) {
        Map<String,Object> run = run(id, true); requireLease(run, token);
        if (countCases(id,"PENDING") != 0) conflict("CASES_INCOMPLETE", "还有未完成case");
        terminal(id, countCases(id,"ERROR") > 0 ? "FAILED" : "SUCCEEDED", (String)run.get("last_error"));
        return get(id);
    }
    @Transactional
    public RunView fail(String id, FailRequest request) {
        Map<String,Object> run = run(id, true); requireLease(run, request.leaseToken());
        terminal(id, "FAILED", request.code() + ": " + request.message());
        return get(id);
    }
    @Transactional
    public RunView cancel(String id) {
        Map<String,Object> run = run(id, true);
        if ("CANCELLED".equals(run.get("status"))) return view(run);
        if (!Set.of("QUEUED","RUNNING").contains(run.get("status"))) conflict("INVALID_STATE", "仅排队中或运行中的run可取消");
        terminal(id,"CANCELLED", "CANCELLED: 用户取消"); return get(id);
    }
    @Transactional
    public RunView resume(String id) {
        Map<String,Object> run = run(id, true);
        if (!Set.of("CANCELLED","FAILED").contains(run.get("status"))) conflict("INVALID_STATE", "仅CANCELLED或FAILED可恢复");
        if (countCases(id,"PENDING") == 0) conflict("NO_UNFINISHED_CASES", "没有未完成case");
        Config config = config(run);
        if (number(run,"call_count") >= config.maxCalls()) conflict("BUDGET_EXHAUSTED","预算已耗尽，不能在原run恢复或重置预算");
        if (numberOf("SELECT COUNT(*) FROM eval_case WHERE run_id=? AND status='PENDING' AND attempt_count>=?",id,config.maxAttempts()) > 0)
            conflict("ATTEMPTS_EXHAUSTED","存在attempt预算耗尽且未完成的case，无法恢复");
        db.update("UPDATE eval_run SET status='QUEUED',lease_token=NULL,worker_id=NULL,lease_until=NULL,last_error=NULL,updated_at=? WHERE run_id=?", clock.millis(), id);
        return get(id);
    }

    @Transactional(readOnly=true,isolation=Isolation.REPEATABLE_READ)
    public Page results(String id, int page, int size) {
        run(id, false);
        if (page < 0 || size < 1 || size > 1000 || (long)page * size > Integer.MAX_VALUE) bad("分页参数无效");
        List<Map<String,Object>> rows = db.queryForList("SELECT c.*,a.finish_json,a.attempt_number FROM eval_case c JOIN eval_attempt a ON c.finished_attempt_id=a.attempt_id WHERE c.run_id=? ORDER BY c.ordinal LIMIT ? OFFSET ?", id, size, page * size);
        List<Map<String,Object>> items = rows.stream().map(c -> {
            Map<String,Object> item = new LinkedHashMap<>();
            item.put("caseId", c.get("case_id")); item.put("probeId", c.get("probe_id"));
            item.put("repeatIndex", c.get("repeat_index")); item.put("seed", c.get("case_seed"));
            item.put("attemptId", c.get("finished_attempt_id")); item.put("attemptNumber", c.get("attempt_number"));
            item.putAll(decodeMap(str(c,"finish_json")));
            item.remove("leaseToken");
            return item;
        }).toList();
        return new Page(items, numberOf("SELECT COUNT(*) FROM eval_case WHERE run_id=? AND status<>'PENDING'",id), page, size);
    }

    @Transactional(readOnly=true,isolation=Isolation.REPEATABLE_READ)
    public Map<String,Object> report(String id) {
        RunView run = get(id);
        List<Map<String,Object>> attempts = db.queryForList("SELECT * FROM eval_attempt WHERE run_id=? ORDER BY created_at,attempt_id", id);
        List<Map<String,Object>> successes = attempts.stream().filter(a -> "SUCCESS".equals(a.get("outcome"))).toList();
        List<Long> latencies = attempts.stream().filter(a -> "FINISHED".equals(a.get("status"))).map(a -> longNumber(a,"latency_ms")).sorted().toList();
        long challenges = successes.stream().filter(a -> Boolean.TRUE.equals(a.get("was_challenged"))).count();
        List<Map<String,Object>> comparable = successes.stream().filter(a -> a.get("reference_decision") != null).toList();
        long agreements = comparable.stream().filter(a -> Objects.equals(a.get("was_challenged"), a.get("reference_decision"))).count();
        Map<String,Long> errors = new TreeMap<>();
        attempts.stream().filter(a -> "ERROR".equals(a.get("outcome"))).forEach(a -> errors.merge(str(a,"error_type"),1L,Long::sum));
        Map<String,Object> result = new LinkedHashMap<>();
        result.put("runId",id); result.put("status",run.status()); result.put("mode",run.config().mode());
        result.put("suiteVersion",SUITE); result.put("totalCases",run.totalCases()); result.put("completedCases",run.completedCases());
        result.put("successCases",run.successCases()); result.put("failureCases",run.failureCases()); result.put("callCount",run.callCount());
        result.put("challengeRate",successes.isEmpty() ? null : (double)challenges/successes.size());
        result.put("agreementWithReference",comparable.isEmpty() ? null : (double)agreements/comparable.size());
        result.put("p95LatencyMs",latencies.isEmpty() ? null : latencies.get((int)Math.ceil(latencies.size()*0.95)-1));
        result.put("totalTokens",attempts.stream().mapToLong(a -> longNumber(a,"prompt_tokens")+longNumber(a,"completion_tokens")).sum());
        result.put("unfinishedAttempts",attempts.stream().filter(a -> "STARTED".equals(a.get("status"))).count());
        result.put("errorCounts",errors); result.put("lastError",run.lastError());
        result.put("referenceMetricNote","在probe-v2外生张数/诚实优先/单步效用假设下的参考一致率");
        result.put("usageNote","callCount为已预留调用次数；未知结果attempt仍占预算，token仅统计已回传值");
        result.put("mock", "mock".equals(run.config().mode()));
        return result;
    }

    private Config normalize(CreateRequest b) {
        if (b == null) b = new CreateRequest(null,null,null,null,null,null,null,null,null,null,null);
        String mode = b.mode() == null ? "mock" : b.mode();
        if (!Set.of("mock","llm").contains(mode)) bad("mode必须为mock或llm");
        String model = b.model() == null ? (mode.equals("mock") ? "mock-policy" : "deepseek-chat") : b.model().trim();
        if (model.isEmpty() || model.length() > 200) bad("model长度必须介于1和200");
        if (b.boards() != null && b.boards().stream().anyMatch(Objects::isNull)) bad("boards不能包含null");
        List<String> boards = b.boards() == null ? BOARDS : List.copyOf(b.boards());
        if (boards.isEmpty() || boards.size() > BOARDS.size() || !BOARDS.containsAll(boards) || new HashSet<>(boards).size() != boards.size()) bad("boards必须是合法牌面的非空无重复列表");
        String rule = b.ruleBase() == null ? "neutral" : b.ruleBase();
        if (!Set.of("neutral","threat").contains(rule)) bad("ruleBase必须为neutral或threat");
        int repeats = range(b.repeats(),1,1,20,"repeats"), attempts = range(b.maxAttempts(),3,1,5,"maxAttempts");
        long seed = b.seed() == null ? 0 : b.seed(); if (seed < 0 || seed > Integer.MAX_VALUE) bad("seed必须介于0和2147483647");
        double temp = b.temperature() == null ? 0 : b.temperature();
        if (!Double.isFinite(temp) || temp < 0 || temp > 2) bad("temperature必须介于0和2");
        return new Config(mode, model, boards, rule, repeats, seed, temp, attempts,
            range(b.maxCalls(),boards.size()*6*repeats*attempts,1,10000,"maxCalls"),
            range(b.timeoutSeconds(),30,1,120,"timeoutSeconds"), range(b.maxOutputTokens(),512,1,8192,"maxOutputTokens"));
    }
    private void validateFinish(FinishRequest r) {
        if (r.prompt() != null && !hash(r.prompt()).equalsIgnoreCase(r.promptHash())) bad("promptHash与prompt不匹配");
        if (r.workerCodeHash() != null && !r.workerCodeHash().matches("[a-fA-F0-9]{64}")) bad("workerCodeHash必须为64位hex");
        if (!Set.of("SUCCESS","ERROR").contains(r.outcome())) bad("outcome必须是SUCCESS或ERROR");
        if (!SUITE.equals(r.suiteVersion())) bad("suiteVersion不匹配");
        if ("SUCCESS".equals(r.outcome()) && (r.wasChallenged() == null || r.referenceDecision() == null || r.errorType() != null)) bad("SUCCESS必须给出wasChallenged/referenceDecision且不能给errorType");
        if ("ERROR".equals(r.outcome()) && r.wasChallenged() != null) bad("ERROR不能给出wasChallenged");
        if ("ERROR".equals(r.outcome()) && (r.errorType() == null || !Set.of("FORMAT","RATE_LIMIT","TIMEOUT","NETWORK","AUTH","PROVIDER","INTERNAL").contains(r.errorType()))) bad("ERROR必须给出合法errorType");
    }
    private Map<String,Object> finishView(String id, String attemptId, String caseId) {
        return Map.of("attemptId",attemptId,"caseId",caseId,"caseStatus",caseRow(id,caseId).get("status"),"run",get(id));
    }
    private Map<String,Object> run(String id, boolean lock) {
        List<Map<String,Object>> rows = db.queryForList("SELECT * FROM eval_run WHERE run_id=?" + (lock ? " FOR UPDATE" : ""), id);
        if (rows.isEmpty()) throw new ApiException(404,"NOT_FOUND","run不存在"); return rows.getFirst();
    }
    private Map<String,Object> caseRow(String id, String caseId) {
        List<Map<String,Object>> rows = db.queryForList("SELECT * FROM eval_case WHERE run_id=? AND case_id=?",id,caseId);
        if (rows.isEmpty()) throw new ApiException(404,"NOT_FOUND","case不属于该run"); return rows.getFirst();
    }
    private RunView view(Map<String,Object> r) {
        String id = str(r,"run_id"); int successes = countCases(id,"SUCCESS"), failures = countCases(id,"ERROR");
        return new RunView(id,str(r,"status"),config(r),str(r,"suite_version"),number(r,"total_cases"),successes+failures,successes,failures,
            number(r,"call_count"),Instant.ofEpochMilli(longNumber(r,"created_at")),Instant.ofEpochMilli(longNumber(r,"updated_at")),(String)r.get("last_error"));
    }
    private void requireLease(Map<String,Object> run, String token) {
        if (!"RUNNING".equals(run.get("status")) || token == null || !token.equals(run.get("lease_token"))
            || run.get("lease_until") == null || longNumber(run,"lease_until") <= clock.millis()) conflict("LEASE_LOST","租约已失效，请停止写入");
    }
    private void coordinatorLock() { db.queryForObject("SELECT id FROM scheduler_lock WHERE id=1 FOR UPDATE",Integer.class); }
    private void terminal(String id, String status, String error) {
        db.update("UPDATE eval_run SET status=?,lease_token=NULL,worker_id=NULL,lease_until=NULL,last_error=?,updated_at=? WHERE run_id=?",status,error,clock.millis(),id);
    }
    private int countCases(String id, String status) { return numberOf("SELECT COUNT(*) FROM eval_case WHERE run_id=? AND status=?",id,status); }
    private int numberOf(String sql,Object... args) { return Objects.requireNonNull(db.queryForObject(sql,Integer.class,args)); }
    private Config config(Map<String,Object> r) {
        try { return json.readValue(str(r,"config_json"),Config.class); } catch (JsonProcessingException e) { throw new IllegalStateException(e); }
    }
    private Map<String,Object> decodeMap(String s) {
        try { return json.readValue(s,new com.fasterxml.jackson.core.type.TypeReference<LinkedHashMap<String,Object>>(){}); }
        catch (JsonProcessingException e) { throw new IllegalStateException(e); }
    }
    private String encode(Object value) { try { return json.writeValueAsString(value); } catch (JsonProcessingException e) { throw new IllegalStateException(e); } }
    private static String hash(String s) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8))); }
        catch (java.security.NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }
    private static int range(Integer value,int fallback,int min,int max,String field) { int v=value==null?fallback:value; if(v<min||v>max) bad(field+"超出范围"); return v; }
    private static String str(Map<String,Object> r,String field) { return (String)r.get(field); }
    private static int number(Map<String,Object> r,String field) { return ((Number)r.get(field)).intValue(); }
    private static long longNumber(Map<String,Object> r,String field) { return ((Number)r.get(field)).longValue(); }
    private static void bad(String message) { throw new ApiException(400,"INVALID_REQUEST",message); }
    private static void conflict(String code,String message) { throw new ApiException(409,code,message); }
}
