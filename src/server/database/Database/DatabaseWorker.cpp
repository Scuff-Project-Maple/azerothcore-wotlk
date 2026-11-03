/*
 * This file is part of the AzerothCore Project. See AUTHORS file for Copyright information
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
 * FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
 * more details.
 *
 * You should have received a copy of the GNU General Public License along
 * with this program. If not, see <http://www.gnu.org/licenses/>.
 */

#include "DatabaseWorker.h"
#include "Config.h"
#include "Log.h"
#include "MySQLConnection.h"
#include "PCQueue.h"
#include "SQLOperation.h"
#include <chrono>

namespace
{
    uint64_t NowMs()
    {
        return static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now().time_since_epoch()
            ).count());
    }
}

DatabaseWorker::DatabaseWorker(ProducerConsumerQueue<SQLOperation*>* newQueue, MySQLConnection* connection)
{
    _connection = connection;
    _queue = newQueue;
    uint32 const stallSeconds = sConfigMgr->GetOption<uint32>("Database.AsyncWorkerStallSeconds", 10);
    _stallThresholdMs = static_cast<uint64_t>(stallSeconds) * 1000;
    _workerThread = std::thread(&DatabaseWorker::WorkerThread, this);
    _watchdogThread = std::thread(&DatabaseWorker::WatchdogThread, this);
}

DatabaseWorker::~DatabaseWorker()
{
    _stop.store(true, std::memory_order_release);
    if (_watchdogThread.joinable())
        _watchdogThread.join();
    _workerThread.join();
}

void DatabaseWorker::WorkerThread()
{
    if (!_queue)
        return;

    for (;;)
    {
        SQLOperation* operation = nullptr;

        _queue->WaitAndPop(operation);

        if (!operation)
            return;

        {
            std::lock_guard<std::mutex> lock(_inFlightMutex);
            _inFlightOp = operation;
            _inFlightSinceMs = NowMs();
        }

        operation->SetConnection(_connection);
        operation->call();

        {
            std::lock_guard<std::mutex> lock(_inFlightMutex);
            _inFlightOp = nullptr;
        }

        delete operation;
    }
}

void DatabaseWorker::WatchdogThread()
{
    while (!_stop.load(std::memory_order_acquire))
    {
        std::this_thread::sleep_for(std::chrono::seconds(2));

        if (_stop.load(std::memory_order_acquire))
            break;

        std::lock_guard<std::mutex> lock(_inFlightMutex);

        if (!_inFlightOp)
            continue;

        uint64_t const elapsedMs = NowMs() - _inFlightSinceMs;
        if (elapsedMs < _stallThresholdMs)
            continue;

        // The operation is acted on while holding _inFlightMutex: the worker
        // takes the same lock before clearing _inFlightOp, so the operation
        // cannot be deleted under the watchdog.
        if (_inFlightOp->SupportsStallResolution())
        {
            LOG_ERROR("sql.driver",
                "DB async worker stalled: in-flight {} on database `{}` has made no progress for {} ms "
                "(threshold {} ms). The operation's future is resolved with a failed result so waiting "
                "callers stop hanging; the worker resumes processing the queue once the blocked MySQL "
                "call returns or the connection is re-established.",
                _inFlightOp->GetOperationName(), _connection->GetDatabaseName(),
                elapsedMs, _stallThresholdMs);
            _inFlightOp->ResolveFutureOnStall();
        }
        else
        {
            LOG_WARN("sql.driver",
                "DB async worker slow: in-flight {} on database `{}` has been running for {} ms "
                "(threshold {} ms); the operation legitimately outlasts the stall threshold and its "
                "future is left pending.",
                _inFlightOp->GetOperationName(), _connection->GetDatabaseName(),
                elapsedMs, _stallThresholdMs);
        }

        // Re-baseline so a still-stuck operation is re-reported once per threshold.
        _inFlightSinceMs = NowMs();
    }
}
