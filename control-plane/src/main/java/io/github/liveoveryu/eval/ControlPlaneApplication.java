package io.github.liveoveryu.eval;

import java.time.Clock;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;

@SpringBootApplication
public class ControlPlaneApplication {
    public static void main(String[] args) { SpringApplication.run(ControlPlaneApplication.class, args); }
    @Bean Clock clock() { return Clock.systemUTC(); }
}
