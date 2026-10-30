val api: String by env
val jaeger: String by env
val receiver: String by env
val karma: String by env

GET("$api/health", name = "API health") then {
    assert(code == 200)
}

GET("$api/fail", name = "Deliberate server error") then {
    assert(code == 500)
    assert(!header("X-Trace-ID").isNullOrBlank())
}

GET("$api/slow", name = "Slow request with nested span") {
    queryParam("seconds", 2)
} then {
    assert(code == 200)
    assert(!header("X-Trace-ID").isNullOrBlank())
}

GET("$jaeger/api/v3/services", name = "Jaeger services") then {
    assert(code == 200)
}

GET("$receiver/events", name = "Received Alertmanager notifications") then {
    assert(code == 200)
}

GET("$karma/health", name = "Karma health") then {
    assert(code == 200)
}
