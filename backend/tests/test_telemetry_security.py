import time
from importlib.metadata import version

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import TraceState, get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from packaging.version import Version


def test_patched_telemetry_versions_and_bounded_tracestate_parsing():
    assert Version(version("opentelemetry-api")) >= Version("1.45.1")
    assert Version(version("opentelemetry-sdk")) >= Version("1.45.1")
    started = time.perf_counter()
    state = TraceState.from_header(["a=b" + " " * 100_000 + "x"])
    elapsed = time.perf_counter() - started
    assert len(state) == 0
    assert elapsed < 1.0, f"Malformed tracestate parsing exceeded 1 second: {elapsed:.3f}"


def test_patched_sdk_span_export_and_trace_context_roundtrip():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        tracer = provider.get_tracer("remedi-compatibility")
        with tracer.start_as_current_span("agent-check") as span:
            carrier = {}
            propagator = TraceContextTextMapPropagator()
            propagator.inject(carrier)
            context = propagator.extract(carrier)
            expected_id = span.get_span_context().trace_id
            assert get_current_span(context).get_span_context().trace_id == expected_id
            assert carrier["traceparent"].split("-")[1] == f"{expected_id:032x}"
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "agent-check"
    finally:
        provider.shutdown()
