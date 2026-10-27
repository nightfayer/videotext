import asyncio
import math
from pathlib import Path
from telethon import TelegramClient
from telethon.tl.functions.upload import GetFileRequest
from telethon.utils import _get_file_info

CHUNK_SIZE = 512 * 1024  # 512 KB per request (Telegram max chunk size)
CONCURRENT_TASKS = 4     # Number of pipelined concurrent chunk requests


async def fast_download_file(
    client: TelegramClient,
    message,
    out_path: Path,
    file_size: int = None,
    progress_callback=None,
    cancel_event: asyncio.Event = None
) -> Path:
    """
    Downloads files from Telegram in high-speed parallel chunks.
    Supports cancellation via cancel_event.
    """
    def check_cancel():
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError("Download cancelled by user")

    check_cancel()

    try:
        file_info = _get_file_info(message)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Fallback to standard download if info cannot be resolved
        res = await client.download_media(message, file=str(out_path), progress_callback=progress_callback)
        return Path(res) if res else out_path

    dc_id = file_info.dc_id
    input_location = file_info.location
    total_size = file_size or file_info.size or 0

    # For files smaller than 10 MB, standard download is already sub-second
    if total_size < 10 * 1024 * 1024 or not dc_id or not input_location:
        res = await client.download_media(message, file=str(out_path), progress_callback=progress_callback)
        return Path(res) if res else out_path

    num_chunks = math.ceil(total_size / CHUNK_SIZE)

    # Pre-allocate file
    with open(out_path, "wb") as f:
        f.truncate(total_size)

    queue = asyncio.Queue()
    for i in range(num_chunks):
        offset = i * CHUNK_SIZE
        # In Telegram MTProto, limit MUST be a multiple of 1024. Using fixed CHUNK_SIZE
        # prevents 'LIMIT_INVALID' errors on the last non-aligned chunk.
        limit = CHUNK_SIZE
        queue.put_nowait((offset, limit))

    downloaded_bytes = 0
    lock = asyncio.Lock()
    sender = None

    async def worker():
        nonlocal downloaded_bytes
        while True:
            check_cancel()
            try:
                offset, limit = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            check_cancel()
            try:
                result = await sender.send(
                    GetFileRequest(input_location, offset=offset, limit=limit)
                )
                check_cancel()
                chunk_bytes = result.bytes if hasattr(result, "bytes") else bytes(result)

                # Ensure chunk does not exceed total file size
                if total_size and (offset + len(chunk_bytes) > total_size):
                    chunk_bytes = chunk_bytes[:total_size - offset]

                with open(out_path, "r+b") as f:
                    f.seek(offset)
                    f.write(chunk_bytes)

                async with lock:
                    downloaded_bytes += len(chunk_bytes)
                    if progress_callback:
                        await progress_callback(downloaded_bytes, total_size)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                queue.put_nowait((offset, limit))
                raise e
            finally:
                queue.task_done()

    same_dc = (dc_id == client.session.dc_id)
    try:
        if same_dc:
            sender = client._sender
        else:
            sender = await client._borrow_exported_sender(dc_id)

        workers = [asyncio.create_task(worker()) for _ in range(CONCURRENT_TASKS)]
        try:
            await asyncio.gather(*workers)
            return out_path
        finally:
            for w in workers:
                if not w.done():
                    w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        check_cancel()
        print(f"[FastDownload] Fallback to standard download due to: {e}", flush=True)
        res = await client.download_media(message, file=str(out_path), progress_callback=progress_callback)
        return Path(res) if res else out_path
    finally:
        if sender is not None and not same_dc:
            try:
                await client._return_exported_sender(sender)
            except Exception:
                pass
