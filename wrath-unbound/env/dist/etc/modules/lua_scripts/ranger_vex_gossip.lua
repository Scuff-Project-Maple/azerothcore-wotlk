--[[ ============================================================
  ranger_vex_gossip.lua — Unbound Wrath Edition
  --------------------------------------------------------------
  Handles:
    • Ranger Vex gossip (flavor only) — T4 rehearsal chain "The Murloc Tide"

  Accept/turn-in are data-driven through the standard quest UI
  (creature_queststarter / creature_questender rows, sibling SQL task).
  This script only adds one selectable option, "What's going on?",
  which prints one of six state texts (spec PRO-5 §2) derived from
  the player's quest status for the two chain quests. No items,
  gold, levels, or quest state are touched.

  NPC entry 900200 = Ranger Vex.
============================================================ --]]

local VEX_ENTRY            = 900200
local QUEST_Q1             = 900101   -- cull five murlocs
local QUEST_Q2             = 900102   -- bring three fins

-- QuestStatus values (src/server/game/Quests/QuestDef.h, enum QuestStatus)
local QUEST_STATUS_NONE       = 0
local QUEST_STATUS_COMPLETE   = 1
local QUEST_STATUS_INCOMPLETE = 3
local QUEST_STATUS_REWARDED   = 6

-- Gossip sender id for the single flavor option.
local SENDER_WHATS_GOING_ON  = 1

-- Header text id for the gossip window (standard default).
local GOSSIP_TEXT_ID         = 100

-- ALE creature gossip event ids (modules/mod-ale/src/LuaEngine/Hooks.h)
local GOSSIP_EVENT_ON_HELLO   = 1
local GOSSIP_EVENT_ON_SELECT  = 2

-- Gossip texts, verbatim from the PRO-5 spec §2 state table.
local TEXT_T0 = "The bluegill tide's been chewing through the dock nets again. Cull five of them for me."
local TEXT_T1 = "How many have you put down so far?"
local TEXT_T2 = "Sounds like you're done. Bring it to me."
local TEXT_T3 = "Bring me three fins when you've got them."
local TEXT_T4 = "One last visit, then I owe you."
local TEXT_T5 = "Good work. The tide's calmer already."

-- Derive the spec state for this player and return the text to print.
-- Falls back to T0 if the quest status cannot be read (never crashes,
-- never error-spams; the standard quest UI is unaffected either way).
local function GetStateText(player)
    local ok1, q1 = pcall(function() return player:GetQuestStatus(QUEST_Q1) end)
    local ok2, q2 = pcall(function() return player:GetQuestStatus(QUEST_Q2) end)
    if not ok1 or not ok2 or q1 == nil or q2 == nil then
        return TEXT_T0
    end

    -- B: Q1 active, not complete.
    if q1 == QUEST_STATUS_INCOMPLETE then
        return TEXT_T1
    end
    -- C: Q1 complete, not turned in.
    if q1 == QUEST_STATUS_COMPLETE then
        return TEXT_T2
    end
    -- D: Q2 active, not complete.
    if q2 == QUEST_STATUS_INCOMPLETE then
        return TEXT_T3
    end
    -- E: Q2 complete, not turned in.
    if q2 == QUEST_STATUS_COMPLETE then
        return TEXT_T4
    end
    -- F: both turned in.
    if q1 == QUEST_STATUS_REWARDED and q2 == QUEST_STATUS_REWARDED then
        return TEXT_T5
    end
    -- Off-table: Q1 turned in but Q2 not active (never accepted, or
    -- abandoned after Q1 reward). Per spec §6 Q1 remains complete and
    -- the next chain step is the fins.
    if q1 == QUEST_STATUS_REWARDED then
        return TEXT_T3
    end
    -- A: nothing active, nothing completed (also covers Q1 FAILED).
    return TEXT_T0
end

local function OnGossipHello(event, player, creature)
    if not player or not creature then
        return
    end
    if not player:IsInWorld() then
        return
    end
    if creature:GetEntry() ~= VEX_ENTRY then
        return
    end

    player:GossipClearMenu()
    -- Standard !/? quest items, data-driven via the quest relations.
    -- No-op if the creature lacks UNIT_NPC_FLAG_QUESTGIVER.
    player:GossipAddQuests(creature)
    player:GossipMenuAddItem(0, "What's going on?", SENDER_WHATS_GOING_ON, 0, false)
    player:GossipSendMenu(GOSSIP_TEXT_ID, creature)
end

local function OnGossipSelect(event, player, creature, sender, intid, code, menuId)
    if not player or not creature then
        return
    end
    if not player:IsInWorld() then
        return
    end
    if sender ~= SENDER_WHATS_GOING_ON then
        -- Not our option. Standard quest offer/turn-in clicks never
        -- arrive here (they use the questgiver opcodes); just close.
        player:GossipComplete()
        return
    end

    local text = GetStateText(player)
    if text then
        player:SendBroadcastMessage(text)
    end
    player:GossipComplete()
end

-- Reload-idempotent registration: BindingMap::Insert appends, so clear
-- any existing bindings for this entry first. A repeated execution of
-- this file in one Lua state then leaves exactly one handler per event.
--
-- ALE validates that the creature entry exists in creature_template at
-- registration time and raises a Lua error otherwise. The Ranger Vex
-- data lands in the sibling SQL task, so while that row is absent this
-- registration is expected to fail at load: log one legible line instead
-- of an ALE error line, and register again on the next load
-- (worldserver restart or .reload ale) once the row exists.
ClearCreatureGossipEvents(VEX_ENTRY, GOSSIP_EVENT_ON_HELLO)
ClearCreatureGossipEvents(VEX_ENTRY, GOSSIP_EVENT_ON_SELECT)

local registered, regErr = pcall(function()
    RegisterCreatureGossipEvent(VEX_ENTRY, GOSSIP_EVENT_ON_HELLO, OnGossipHello)
    RegisterCreatureGossipEvent(VEX_ENTRY, GOSSIP_EVENT_ON_SELECT, OnGossipSelect)
end)

if registered then
    print(string.format("[UNBOUND] ranger_vex_gossip.lua loaded: entry %d, quests %d/%d",
        VEX_ENTRY, QUEST_Q1, QUEST_Q2))
else
    print(string.format("[UNBOUND] ranger_vex_gossip.lua: registration skipped for entry %d: %s",
        VEX_ENTRY, tostring(regErr)))
end
