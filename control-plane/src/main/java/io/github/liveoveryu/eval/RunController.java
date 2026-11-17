package io.github.liveoveryu.eval;

import static io.github.liveoveryu.eval.Protocol.*;
import jakarta.validation.Valid;
import java.util.List;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
class RunController {
    private final RunService runs;
    RunController(RunService runs) { this.runs = runs; }
    @PostMapping("/runs") ResponseEntity<RunView> create(@RequestHeader(value="Idempotency-Key",required=false) String key,
        @RequestBody(required=false) CreateRequest body) {
        CreateResult result = runs.create(key,body); return ResponseEntity.status(result.created()?201:200).body(result.run());
    }
    @GetMapping("/runs") List<RunView> list(@RequestParam(defaultValue="20") int limit) { return runs.list(limit); }
    @GetMapping("/runs/{id}") RunView get(@PathVariable String id) { return runs.get(id); }
    @GetMapping("/runs/{id}/results") Page results(@PathVariable String id,@RequestParam(defaultValue="0") int page,@RequestParam(defaultValue="100") int size) { return runs.results(id,page,size); }
    @GetMapping("/runs/{id}/report") Map<String,Object> report(@PathVariable String id) { return runs.report(id); }
    @PostMapping("/runs/{id}/cancel") RunView cancel(@PathVariable String id) { return runs.cancel(id); }
    @PostMapping("/runs/{id}/resume") RunView resume(@PathVariable String id) { return runs.resume(id); }
    @PostMapping("/worker/claims") ResponseEntity<Claim> claim(@Valid @RequestBody ClaimRequest request) {
        Claim claim = runs.claim(request.workerId()); return claim == null ? ResponseEntity.noContent().build() : ResponseEntity.ok(claim);
    }
    @PostMapping("/worker/runs/{id}/heartbeat") LeaseView heartbeat(@PathVariable String id,@Valid @RequestBody LeaseRequest request) { return runs.heartbeat(id,request.leaseToken()); }
    @PostMapping("/worker/runs/{id}/attempts") ResponseEntity<AttemptView> reserve(@PathVariable String id,@Valid @RequestBody AttemptRequest request) {
        AttemptResult result = runs.reserve(id,request); return ResponseEntity.status(result.created()?201:200).body(result.attempt());
    }
    @PostMapping("/worker/runs/{id}/attempts/{attemptId}/finish") Map<String,Object> finish(@PathVariable String id,@PathVariable String attemptId,@Valid @RequestBody FinishRequest request) { return runs.finish(id,attemptId,request); }
    @PostMapping("/worker/runs/{id}/complete") RunView complete(@PathVariable String id,@Valid @RequestBody LeaseRequest request) { return runs.complete(id,request.leaseToken()); }
    @PostMapping("/worker/runs/{id}/fail") RunView fail(@PathVariable String id,@Valid @RequestBody FailRequest request) { return runs.fail(id,request); }
}
