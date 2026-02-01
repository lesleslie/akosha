# Graceful Shutdown Handler

When Akosha receives a shutdown signal (SIGTERM or SIGINT), it should gracefully stop all services with a 30-second drain period to allow in-flight uploads to complete.

## Implementation

The `AkoshaApplication` class manages the application lifecycle:

```python
class AkoshaApplication:
    def __init__(self):
        self.shutdown_event = asyncio.Event()
        self.ingestion_workers = []

    def _handle_shutdown(self, signum, frame):
        """Handle shutdown signal - sets event and logs."""
        logger.info(f"Received signal {signum}, initiating graceful shutdown")
        self.shutdown_event.set()

    async def stop(self):
        """Stop services with 30s drain period."""
        logger.info("Stopping Akosha services (30s drain period)")

        # Stop workers (they complete in-flight work)
        for worker in self.ingestion_workers:
            worker.stop()

        # Wait for shutdown or timeout
        try:
            await asyncio.wait_for(self.shutdown_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("Graceful shutdown timed out, forcing exit")

        logger.info("Akosha services stopped")
```

## Signals Handled

- **SIGTERM (15)**: Graceful termination (e.g., Kubernetes pod termination)
- **SIGINT (2)**: Interrupt from keyboard (Ctrl+C)

## Drain Period Behavior

1. **Workers stop accepting new work**: Sets `_running = False`
2. **In-flight uploads complete**: Workers finish current `_process_upload()` calls
3. **30-second timeout**: If work not complete after 30s, force shutdown
4. **Clean shutdown**: All connections closed, resources released

## Testing Graceful Shutdown

```bash
# Send SIGTERM to test graceful shutdown
kill -TERM $(pgrep -f "python -m akosha.main")

# Or Ctrl+C for SIGINT
```

## Monitoring

During shutdown, watch for these log messages:
- `"Received signal 15, initiating graceful shutdown"`
- `"Stopping Akosha services (30s drain period)"`
- `"Ingestion worker stopped"` (from each worker)
- `"Akosha services stopped"` or `"Graceful shutdown timed out"`

## Troubleshooting

**Problem**: Shutdown takes longer than 30 seconds
- **Cause**: Long-running uploads not completing
- **Solution**: Check upload processing times, increase timeout if needed

**Problem**: Workers not stopping
- **Cause**: Deadlock or blocking operation
- **Solution**: Check worker logs, ensure async/await used correctly

**Problem**: Data loss during shutdown
- **Cause**: Forced timeout before uploads complete
- **Solution**: Verify `worker.stop()` allows completion of current task
