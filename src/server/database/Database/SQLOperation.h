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

#ifndef _SQLOPERATION_H
#define _SQLOPERATION_H

#include "DatabaseEnvFwd.h"
#include "Define.h"
#include <atomic>
#include <string_view>
#include <variant>

//- Type specifier of our element data
enum SQLElementDataType
{
    SQL_ELEMENT_RAW,
    SQL_ELEMENT_PREPARED
};

//- The element
struct SQLElementData
{
    std::variant<PreparedStatementBase*, std::string> element;
    SQLElementDataType type;
};

class MySQLConnection;

class AC_DATABASE_API SQLOperation
{
public:
    SQLOperation() = default;
    virtual ~SQLOperation() = default;

    virtual int call()
    {
        Execute();
        return 0;
    }

    virtual bool Execute() = 0;
    virtual void SetConnection(MySQLConnection* con) { m_conn = con; }

    //! Resolves this operation's future with an empty/failed value so waiting
    //! callers do not hang forever. Thread-safe and idempotent: the first
    //! resolver (the executing worker, or the async worker's watchdog when the
    //! operation made no progress for the stall threshold) wins.
    //! Operations without a future do nothing.
    virtual void ResolveFutureOnStall() { }

    //! False when a long in-flight execution is legitimate (e.g. the deadlock
    //! retry loop) and the watchdog must not force-resolve the future.
    virtual bool SupportsStallResolution() const { return true; }

    //! Short human-readable operation type for diagnostics (stall logging).
    virtual std::string_view GetOperationName() const { return "SQLOperation"; }

    MySQLConnection* m_conn{nullptr};

private:
    std::atomic<bool> _completionClaimed{ false };

    SQLOperation(SQLOperation const& right) = delete;
    SQLOperation& operator=(SQLOperation const& right) = delete;

protected:
    //! Atomically claims the right to resolve this operation's future.
    //! Exactly one of the executing worker or the watchdog wins; the loser
    //! must skip its own set_value() call to avoid a double resolve.
    bool ClaimCompletion()
    {
        return _completionClaimed.exchange(true, std::memory_order_acq_rel) == false;
    }
};

#endif
