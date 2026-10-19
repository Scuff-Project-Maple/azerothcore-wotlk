/*
 * mod-custom-classes
 *
 * Generic, data-driven custom class system for Wrath Unbound.
 *
 * - Registry (WorldScript) loads the `custom_class_template` table from the
 *   world DB in two stages:
 *     stage 1 (OnAfterConfigLoad): schema, JSON, ranges, mask allocation;
 *     stage 2 (OnBeforeWorldInitialized): cross-validation against the DBC
 *   stores (talent tabs, spells, items, creature families).
 *   A broken row disables that one template with a single clear error line
 *   naming the template and the field; it never crashes the server.
 * - CustomClassPlayerScript applies the template's curves, spec gating,
 *   starting kit, proficiencies, LFG roles and its bit in the unbound class
 *   mask. Every handler is a no-op when the player's class has no template,
 *   so with zero templates the server behaves exactly like vanilla.
 *
 * Contract: docs/custom-class-template.md
 */

#include "CustomClassSystem.h"

#include "Config.h"
#include "DataStores/DBCStructure.h"
#include "DatabaseEnv.h"
#include "DBCStores.h"
#include "DungeonFinding/LFG.h"
#include "Entities/Creature/Trainer.h"
#include "Entities/Item/ItemTemplate.h"
#include "Log.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "ScriptMgr.h"
#include "ScriptDefines/PlayerScript.h"
#include "ScriptDefines/WorldScript.h"
#include "SpellMgr.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace
{
    // ---------------------------------------------------------------------
    // Minimal JSON subset parser (objects, arrays, numbers, strings,
    // true/false/null; no comments, no trailing commas, no \u escapes).
    // Anything outside the subset is a load-time failure for that row.
    // ---------------------------------------------------------------------

    struct JsonNode
    {
        enum class Type { Null, Bool, Number, String, Array, Object };
        Type type = Type::Null;
        bool boolean = false;
        double number = 0.0;
        std::string string;
        std::vector<JsonNode> items;
        struct Member;
        std::vector<Member> members;

        JsonNode const* Find(std::string const& key) const;
    };

    struct JsonNode::Member
    {
        std::string first;
        JsonNode second;
    };

    JsonNode const* JsonNode::Find(std::string const& key) const
    {
        if (type != Type::Object)
            return nullptr;
        for (auto const& member : members)
            if (member.first == key)
                return &member.second;
        return nullptr;
    }

    bool IsWholeNumber(double value)
    {
        return std::floor(value) == value;
    }

    class JsonParser
    {
    public:
        JsonParser(std::string const& text, std::string& err) : m_text(text), m_err(err) {}

        bool Parse(JsonNode& out)
        {
            SkipWs();
            if (!ParseValue(out))
                return false;
            SkipWs();
            if (m_pos != m_text.size())
                Fail("unexpected trailing content after JSON value");
            return m_err.empty();
        }

    private:
        void Fail(std::string const& message)
        {
            if (m_err.empty())
                m_err = "JSON parse error at offset " + std::to_string(m_pos) + ": " + message;
        }

        void SkipWs()
        {
            while (m_pos < m_text.size())
            {
                char const c = m_text[m_pos];
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r')
                    ++m_pos;
                else
                    break;
            }
        }

        bool Match(char expected)
        {
            if (m_pos < m_text.size() && m_text[m_pos] == expected)
            {
                ++m_pos;
                return true;
            }
            return false;
        }

        bool ParseValue(JsonNode& out)
        {
            SkipWs();
            if (m_pos >= m_text.size())
            {
                Fail("unexpected end of input");
                return false;
            }
            char const c = m_text[m_pos];
            switch (c)
            {
                case '{': return ParseObject(out);
                case '[': return ParseArray(out);
                case '"':
                {
                    out.type = JsonNode::Type::String;
                    return ParseString(out.string);
                }
                case 't':
                    if (m_text.compare(m_pos, 4, "true") == 0)
                    {
                        out.type = JsonNode::Type::Bool;
                        out.boolean = true;
                        m_pos += 4;
                        return true;
                    }
                    Fail("invalid literal");
                    return false;
                case 'f':
                    if (m_text.compare(m_pos, 5, "false") == 0)
                    {
                        out.type = JsonNode::Type::Bool;
                        out.boolean = false;
                        m_pos += 5;
                        return true;
                    }
                    Fail("invalid literal");
                    return false;
                case 'n':
                    if (m_text.compare(m_pos, 4, "null") == 0)
                    {
                        out.type = JsonNode::Type::Null;
                        m_pos += 4;
                        return true;
                    }
                    Fail("invalid literal");
                    return false;
                default:
                    return ParseNumber(out);
            }
        }

        bool ParseNumber(JsonNode& out)
        {
            char* end = nullptr;
            double const value = std::strtod(m_text.c_str() + m_pos, &end);
            if (end == m_text.c_str() + m_pos)
            {
                Fail("invalid number");
                return false;
            }
            out.type = JsonNode::Type::Number;
            out.number = value;
            m_pos += static_cast<size_t>(end - (m_text.c_str() + m_pos));
            return true;
        }

        bool ParseObject(JsonNode& out)
        {
            out.type = JsonNode::Type::Object;
            if (!Match('{'))
            {
                Fail("expected '{'");
                return false;
            }
            SkipWs();
            if (Match('}'))
                return true;
            while (true)
            {
                SkipWs();
                if (m_pos >= m_text.size() || m_text[m_pos] != '"')
                {
                    Fail("expected object key string");
                    return false;
                }
                std::string key;
                if (!ParseString(key))
                    return false;
                SkipWs();
                if (!Match(':'))
                {
                    Fail("expected ':' after object key");
                    return false;
                }
                JsonNode value;
                if (!ParseValue(value))
                    return false;
                out.members.emplace_back(std::move(key), std::move(value));
                SkipWs();
                if (Match(','))
                    continue;
                if (Match('}'))
                    return true;
                Fail("expected ',' or '}' in object");
                return false;
            }
        }

        bool ParseArray(JsonNode& out)
        {
            out.type = JsonNode::Type::Array;
            if (!Match('['))
            {
                Fail("expected '['");
                return false;
            }
            SkipWs();
            if (Match(']'))
                return true;
            while (true)
            {
                JsonNode item;
                if (!ParseValue(item))
                    return false;
                out.items.push_back(std::move(item));
                SkipWs();
                if (Match(','))
                    continue;
                if (Match(']'))
                    return true;
                Fail("expected ',' or ']' in array");
                return false;
            }
        }

        bool ParseString(std::string& out)
        {
            if (!Match('"'))
            {
                Fail("expected string");
                return false;
            }
            while (m_pos < m_text.size())
            {
                char const c = m_text[m_pos++];
                if (c == '"')
                    return true;
                if (c == '\\')
                {
                    if (m_pos >= m_text.size())
                        break;
                    char const esc = m_text[m_pos++];
                    switch (esc)
                    {
                        case '"': out.push_back('"'); break;
                        case '\\': out.push_back('\\'); break;
                        case '/': out.push_back('/'); break;
                        case 'b': out.push_back('\b'); break;
                        case 'f': out.push_back('\f'); break;
                        case 'n': out.push_back('\n'); break;
                        case 'r': out.push_back('\r'); break;
                        case 't': out.push_back('\t'); break;
                        default:
                            Fail("unsupported escape sequence");
                            return false;
                    }
                    continue;
                }
                out.push_back(c);
            }
            Fail("unterminated string");
            return false;
        }

        std::string const& m_text;
        std::string& m_err;
        size_t m_pos = 0;
    };

    // Parses one `{ "<level>": <value>, ... }` object into a StatCurve.
    // A null/absent node is OK (optional sub-curve).
    void ParseLevelCurveObject(JsonNode const* node, CustomClasses::StatCurve& curve,
        std::string const& field, std::string& err)
    {
        if (!node || node->type == JsonNode::Type::Null)
            return;
        if (node->type != JsonNode::Type::Object)
        {
            err = field + ": expected JSON object {\"level\": value}";
            return;
        }
        for (auto const& member : node->members)
        {
            char* keyEnd = nullptr;
            long const level = std::strtol(member.first.c_str(), &keyEnd, 10);
            if (keyEnd == member.first.c_str() || *keyEnd != '\0')
            {
                err = field + ": level key \"" + member.first + "\" is not a whole number";
                return;
            }
            if (level < 1 || level > CustomClasses::MaxPlayerLevel)
            {
                err = field + ": level " + std::to_string(level) + " outside 1-" +
                    std::to_string(CustomClasses::MaxPlayerLevel);
                return;
            }
            if (member.second.type != JsonNode::Type::Number ||
                member.second.number < 0.0 || !IsWholeNumber(member.second.number))
            {
                err = field + ": value at level " + std::to_string(level) +
                    " must be a non-negative whole number";
                return;
            }
            curve.SetValue(static_cast<uint8>(level), static_cast<uint32>(member.second.number));
        }
        if (curve.empty())
        {
            err = field + ": no level entries";
            return;
        }
    }

    // Parses a column that is either empty or a `{ "level": value, ... }` object.
    void ParseCurveColumn(std::string const& text, CustomClasses::StatCurve& curve,
        std::string const& field, std::string& err)
    {
        if (text.empty())
            return;
        JsonNode root;
        JsonParser parser(text, err);
        if (!parser.Parse(root))
            return;
        ParseLevelCurveObject(&root, curve, field, err);
    }

    // Parses the ap_curve column: { "melee": {...}, "ranged": {...} }, melee required.
    void ParseApCurve(std::string const& text, CustomClasses::ApCurve& out, std::string& err)
    {
        if (text.empty())
            return;
        JsonNode root;
        JsonParser parser(text, err);
        if (!parser.Parse(root))
            return;
        if (root.type != JsonNode::Type::Object)
        {
            err = "ap_curve: expected { \"melee\": {...}, \"ranged\": {...} }";
            return;
        }
        JsonNode const* melee = root.Find("melee");
        JsonNode const* ranged = root.Find("ranged");
        if (!melee && !ranged)
        {
            err = "ap_curve: must define at least a \"melee\" curve";
            return;
        }
        std::string subErr;
        if (melee)
        {
            ParseLevelCurveObject(melee, out.melee, "ap_curve.melee", subErr);
            if (!subErr.empty())
            {
                err = subErr;
                return;
            }
        }
        if (ranged)
        {
            ParseLevelCurveObject(ranged, out.ranged, "ap_curve.ranged", subErr);
            if (!subErr.empty())
            {
                err = subErr;
                return;
            }
        }
    }

    // Parses an array of whole numbers >= 1. Absent/null = empty (OK).
    void ParseIdArray(JsonNode const* node, std::vector<uint32>& out,
        std::string const& field, std::string& err)
    {
        if (!node || node->type == JsonNode::Type::Null)
            return;
        if (node->type != JsonNode::Type::Array)
        {
            err = field + ": expected JSON array of whole numbers";
            return;
        }
        for (size_t i = 0; i < node->items.size(); ++i)
        {
            JsonNode const& item = node->items.at(i);
            if (item.type != JsonNode::Type::Number ||
                !IsWholeNumber(item.number) || item.number < 1.0)
            {
                err = field + "[" + std::to_string(i) + "]: each id must be a whole number >= 1";
                return;
            }
            out.push_back(static_cast<uint32>(item.number));
        }
    }

    void ParseSpecs(std::string const& text, std::vector<CustomClasses::Spec>& out,
        std::string& err)
    {
        if (text.empty())
            return;
        JsonNode root;
        JsonParser parser(text, err);
        if (!parser.Parse(root))
            return;
        if (root.type != JsonNode::Type::Array)
        {
            err = "specs: expected JSON array";
            return;
        }
        for (size_t i = 0; i < root.items.size(); ++i)
        {
            JsonNode const& node = root.items.at(i);
            if (node.type != JsonNode::Type::Object)
            {
                err = "specs[" + std::to_string(i) + "]: expected object";
                return;
            }
            CustomClasses::Spec spec;
            JsonNode const* tree = node.Find("talent_tree");
            if (!tree || tree->type != JsonNode::Type::Number ||
                !IsWholeNumber(tree->number) || tree->number < 0.0 || tree->number >= 1e9)
            {
                err = "specs[" + std::to_string(i) + "].talent_tree: missing or not a non-negative whole number";
                return;
            }
            spec.talentTree = static_cast<uint32>(tree->number);
            JsonNode const* roleNode = node.Find("role");
            std::string const role =
                (roleNode && roleNode->type == JsonNode::Type::String) ? roleNode->string : std::string();
            if (role == "tank")
                spec.role = CustomClasses::SpecRole::Tank;
            else if (role == "healer")
                spec.role = CustomClasses::SpecRole::Healer;
            else if (role == "damage")
                spec.role = CustomClasses::SpecRole::Damage;
            else
            {
                err = "specs[" + std::to_string(i) + "].role: must be \"tank\", \"healer\" or \"damage\"";
                return;
            }
            ParseIdArray(node.Find("spell_whitelist"), spec.spellWhitelist,
                "specs[" + std::to_string(i) + "].spell_whitelist", err);
            if (!err.empty())
                return;
            out.push_back(spec);
        }
    }

    void ParseStartingKit(std::string const& text, CustomClasses::StartingKit& kit, std::string& err)
    {
        if (text.empty())
            return;
        JsonNode root;
        JsonParser parser(text, err);
        if (!parser.Parse(root))
            return;
        if (root.type != JsonNode::Type::Object)
        {
            err = "starting_kit: expected JSON object";
            return;
        }
        ParseIdArray(root.Find("spells"), kit.spells, "starting_kit.spells", err);
        if (!err.empty())
            return;
        ParseIdArray(root.Find("items"), kit.items, "starting_kit.items", err);
        if (!err.empty())
            return;
        ParseIdArray(root.Find("action_bar"), kit.actionBar, "starting_kit.action_bar", err);
    }

    // Stage-1 validation (no DBC): field ranges + allocation rules.
    std::string ValidateStage1(CustomClasses::Template const& t,
        std::unordered_set<uint32> const& usedMaskBits, std::unordered_set<uint8> const& usedBackings)
    {
        if (t.key.empty())
            return "template_key is empty";
        if (t.key.length() > 64)
            return "template_key longer than 64 characters";
        if (t.backingClass < 1 || t.backingClass == 10 || t.backingClass > 11)
            return "backing_class " + std::to_string(t.backingClass) +
                " is not a playable vanilla class (1-9 or 11)";
        if (usedBackings.count(t.backingClass) != 0)
            return "backing_class " + std::to_string(t.backingClass) +
                " is already used by another template (one template per backing class)";
        if (t.primaryStat >= MAX_STATS)
            return "primary_stat " + std::to_string(t.primaryStat) + " out of range 0-4";
        if (t.unboundMaskBit < CustomClasses::MaskBitLow || t.unboundMaskBit > CustomClasses::MaskBitHigh)
            return "unbound_mask_bit " + std::to_string(t.unboundMaskBit) +
                " outside the allocation range [" + std::to_string(CustomClasses::MaskBitLow) +
                ", " + std::to_string(CustomClasses::MaskBitHigh) + "]";
        if ((t.unboundMaskBit & (t.unboundMaskBit - 1)) != 0)
            return "unbound_mask_bit " + std::to_string(t.unboundMaskBit) + " is not a single bit";
        if (usedMaskBits.count(t.unboundMaskBit) != 0)
            return "unbound_mask_bit " + std::to_string(t.unboundMaskBit) +
                " is already allocated to another template";
        if (t.weaponProfMask >= (1u << MAX_ITEM_SUBCLASS_WEAPON))
            return "weapon_prof_mask " + std::to_string(t.weaponProfMask) +
                " references item sub-classes beyond the weapon range";
        if (t.armorProfMask >= (1u << MAX_ITEM_SUBCLASS_ARMOR))
            return "armor_prof_mask " + std::to_string(t.armorProfMask) +
                " references item sub-classes beyond the armor range";
        return std::string();
    }

    // Idempotent proficiency grant (login + first login).
    void ApplyProficiencies(Player* player, CustomClasses::Template const& t)
    {
        if (t.weaponProfMask != 0 &&
            (player->GetWeaponProficiency() & t.weaponProfMask) != t.weaponProfMask)
        {
            player->AddWeaponProficiency(t.weaponProfMask);
            player->SendProficiency(ITEM_CLASS_WEAPON, player->GetWeaponProficiency());
        }
        if (t.armorProfMask != 0 &&
            (player->GetArmorProficiency() & t.armorProfMask) != t.armorProfMask)
        {
            player->AddArmorProficiency(t.armorProfMask);
            player->SendProficiency(ITEM_CLASS_ARMOR, player->GetArmorProficiency());
        }
    }

    // First-login starting kit: learn missing spells, store items in best slots.
    void GrantStartingKit(Player* player, CustomClasses::Template const& t)
    {
        for (uint32 spellId : t.startingKit.spells)
            if (!player->HasSpell(spellId))
                player->learnSpell(spellId, false, false);
        for (uint32 itemId : t.startingKit.items)
            if (sObjectMgr->GetItemTemplate(itemId))
                player->StoreNewItemInBestSlots(itemId, 1);
    }
}

namespace CustomClasses
{
    Registry::Registry()
        : WorldScript("CustomClassRegistry",
              { WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_BEFORE_WORLD_INITIALIZED })
    {
    }

    Template const* Registry::GetTemplate(uint8 backingClass) const
    {
        auto const it = m_byClass.find(backingClass);
        return it != m_byClass.end() ? &it->second : nullptr;
    }

    Template const* Registry::GetPlayerTemplate(Player const* player)
    {
        if (!player || !m_enabled || m_byClass.empty())
            return nullptr;
        if (player->GetSession() && player->GetSession()->IsBot())
            return nullptr;
        return GetTemplate(player->getClass());
    }

    void Registry::OnAfterConfigLoad(bool reload)
    {
        m_enabled = sConfigMgr->GetOption<bool>("CustomClasses.Enabled", true);
        m_byClass.clear();
        if (!m_enabled)
        {
            LOG_INFO("worldserver.custom-classes", "CustomClasses.Enabled=false; registry stays empty (vanilla no-op)");
            return;
        }
        LoadFromDatabase();
        if (reload)
        {
            // RELOAD CONFIG: DBC stores are already live, so run stage 2 immediately.
            ValidateAgainstDBC();
        }
    }

    void Registry::OnBeforeWorldInitialized()
    {
        if (m_enabled && !m_byClass.empty())
            ValidateAgainstDBC();
    }

    void Registry::LoadFromDatabase()
    {
        QueryResult const result = WorldDatabase.Query(
            "SELECT template_key, display_name, backing_class, color, icon, primary_stat, "
            "health_curve, mana_curve, ap_curve, spellpower_curve, crit_curve, specs, "
            "starting_kit, pet_family, weapon_prof_mask, armor_prof_mask, unbound_mask_bit, "
            "enabled, description FROM custom_class_template");

        if (!result)
        {
            LOG_ERROR("worldserver.custom-classes",
                "Table `custom_class_template` is missing or unreadable; apply "
                "wrath-unbound/modules/mod-custom-classes/data/sql/db-world/01_custom_class_template.sql");
            return;
        }

        std::unordered_set<uint32> usedMaskBits;
        std::unordered_set<uint8> usedBackings;
        int rows = 0;
        int disabled = 0;
        int failed = 0;

        do
        {
            Field const* fields = result->Fetch();
            Template t;
            t.key = fields[0].Get<std::string>();
            t.displayName = fields[1].Get<std::string>();
            t.backingClass = fields[2].Get<uint8>();
            t.color = fields[3].Get<std::string>();
            t.icon = fields[4].Get<std::string>();
            t.primaryStat = fields[5].Get<uint8>();
            t.petFamily = fields[13].Get<uint16>();
            t.weaponProfMask = fields[14].Get<uint32>();
            t.armorProfMask = fields[15].Get<uint32>();
            t.unboundMaskBit = fields[16].Get<uint32>();
            t.enabled = fields[17].Get<uint8>() != 0;

            ++rows;
            if (!t.enabled)
            {
                ++disabled;
                continue;
            }

            std::string err;
            ParseCurveColumn(fields[6].Get<std::string>(), t.health, "health_curve", err);
            if (err.empty())
                ParseCurveColumn(fields[7].Get<std::string>(), t.mana, "mana_curve", err);
            if (err.empty())
                ParseApCurve(fields[8].Get<std::string>(), t.ap, err);
            if (err.empty())
                ParseCurveColumn(fields[9].Get<std::string>(), t.spellpower, "spellpower_curve", err);
            if (err.empty())
                ParseCurveColumn(fields[10].Get<std::string>(), t.crit, "crit_curve", err);
            if (err.empty())
                ParseSpecs(fields[11].Get<std::string>(), t.specs, err);
            if (err.empty())
                ParseStartingKit(fields[12].Get<std::string>(), t.startingKit, err);
            if (err.empty())
                err = ValidateStage1(t, usedMaskBits, usedBackings);
            if (!err.empty())
            {
                ++failed;
                LOG_ERROR("worldserver.custom-classes", "Template \"{}\" disabled: {}", t.key, err);
                continue;
            }

            usedMaskBits.insert(t.unboundMaskBit);
            usedBackings.insert(t.backingClass);
            m_byClass[t.backingClass] = std::move(t);
        }
        while (result->NextRow());

        LOG_INFO("worldserver.custom-classes",
            "custom_class_template: {} row(s) read, {} template(s) active, {} disabled by flag, {} rejected at load",
            rows, m_byClass.size(), disabled, failed);
    }

    void Registry::ValidateAgainstDBC()
    {
        std::unordered_set<uint32> existingTalents;
        for (auto const& item : sTalentStore)
            existingTalents.insert(item->TalentID);

        int removed = 0;
        for (auto it = m_byClass.begin(); it != m_byClass.end();)
        {
            std::string err;
            if (!ValidateTemplateAgainstDBC(it->second, existingTalents, err))
            {
                LOG_ERROR("worldserver.custom-classes", "Template \"{}\" disabled after DBC validation: {}",
                    it->second.key, err);
                ++removed;
                it = m_byClass.erase(it);
                continue;
            }
            ComputeBlockedSpells(it->second);
            ++it;
        }

        LOG_INFO("worldserver.custom-classes",
            "custom class registry ready: {} template(s) active ({} removed after DBC validation)",
            m_byClass.size(), removed);
    }

    bool Registry::ValidateTemplateAgainstDBC(Template const& t,
        std::unordered_set<uint32> const& existingTalents, std::string& err) const
    {
        uint32 const classBit = 1u << (t.backingClass - 1);
        for (auto const& spec : t.specs)
        {
            TalentTabEntry const* tab = sTalentTabStore.LookupEntry(spec.talentTree);
            if (!tab)
            {
                err = "spec talent_tree " + std::to_string(spec.talentTree) +
                    " not found in TalentTab.dbc";
                return false;
            }
            if ((tab->ClassMask & classBit) == 0)
            {
                err = "spec talent_tree " + std::to_string(spec.talentTree) +
                    " is not offered to backing class " + std::to_string(t.backingClass);
                return false;
            }
            for (uint32 id : spec.spellWhitelist)
                if (existingTalents.find(id) == existingTalents.end())
                {
                    err = "spec spell_whitelist contains unknown talent id " + std::to_string(id);
                    return false;
                }
        }
        for (uint32 spell : t.startingKit.spells)
            if (!sSpellMgr->GetSpellInfo(spell))
            {
                err = "starting_kit.spells contains unknown spell id " + std::to_string(spell);
                return false;
            }
        for (uint32 item : t.startingKit.items)
            if (!sObjectMgr->GetItemTemplate(item))
            {
                err = "starting_kit.items contains unknown item id " + std::to_string(item);
                return false;
            }
        if (t.petFamily != 0 && !sCreatureFamilyStore.LookupEntry(t.petFamily))
        {
            err = "pet_family " + std::to_string(t.petFamily) + " not found in CreatureFamily.dbc";
            return false;
        }
        return true;
    }

    // Builds blockedSpells = (spells of the talent tabs the template covers for
    // its backing class) - (spells of the whitelisted portions) - (kit spells).
    void Registry::ComputeBlockedSpells(Template& t) const
    {
        t.blockedSpells.clear();
        if (t.specs.empty())
            return;

        uint32 const classBit = 1u << (t.backingClass - 1);
        std::unordered_set<uint32> pool;
        std::unordered_set<uint32> allowed;
        for (uint32 spell : t.startingKit.spells)
            allowed.insert(spell);

        for (auto const& item : sTalentStore)
        {
            TalentEntry const* entry = item;
            TalentTabEntry const* tab = sTalentTabStore.LookupEntry(entry->TalentTab);
            if (!tab || (tab->ClassMask & classBit) == 0)
                continue;

            bool const covered = std::any_of(t.specs.begin(), t.specs.end(),
                [&](Spec const& s) { return s.talentTree == entry->TalentTab; });
            if (!covered)
                continue;

            pool.insert(entry->TalentID);
            for (uint32 rankSpell : entry->RankID)
                if (rankSpell != 0)
                    pool.insert(rankSpell);
            if (entry->addToSpellBook != 0)
                pool.insert(entry->addToSpellBook);

            for (auto const& s : t.specs)
            {
                if (s.talentTree != entry->TalentTab || !s.spellWhitelisted(entry->TalentID))
                    continue;
                allowed.insert(entry->TalentID);
                for (uint32 rankSpell : entry->RankID)
                    if (rankSpell != 0)
                        allowed.insert(rankSpell);
                if (entry->addToSpellBook != 0)
                    allowed.insert(entry->addToSpellBook);
            }
        }

        for (uint32 spell : pool)
            if (allowed.find(spell) == allowed.end())
                t.blockedSpells.insert(spell);
    }

    bool SpellAllowed(Template const& t, uint32 spellId)
    {
        if (t.specs.empty())
            return true;
        for (uint32 spell : t.startingKit.spells)
            if (spell == spellId)
                return true;
        return t.blockedSpells.find(spellId) == t.blockedSpells.end();
    }

    Registry* GetRegistryForHooks();

    class CustomClassPlayerScript : public PlayerScript
    {
    public:
        CustomClassPlayerScript()
            : PlayerScript("CustomClassPlayerScript",
                  { PLAYERHOOK_ON_LOGIN, PLAYERHOOK_ON_FIRST_LOGIN,
                    PLAYERHOOK_ON_PLAYER_ONLINE,
                    PLAYERHOOK_ON_AFTER_UPDATE_MAX_HEALTH,
                    PLAYERHOOK_ON_AFTER_UPDATE_MAX_POWER,
                    PLAYERHOOK_ON_AFTER_UPDATE_ATTACK_POWER_AND_DAMAGE,
                    PLAYERHOOK_ON_AFTER_UPDATE_ALL_STATS,
                    PLAYERHOOK_ON_PLAYER_IS_CLASS,
                    PLAYERHOOK_ON_PLAYER_LFG_SET_ROLES,
                    PLAYERHOOK_CAN_LEARN_TALENT,
                    PLAYERHOOK_CAN_LEARN_SPELL,
                    PLAYERHOOK_ON_GET_TRAINER_SPELL_STATE })
        {
        }

    private:
        static Template const* TemplateOf(Player const* player)
        {
            // The player script has no pointer to the registry; resolution is
            // class-level, so go through the world script registry singleton.
            return GetRegistryForHooks() ? GetRegistryForHooks()->GetPlayerTemplate(player) : nullptr;
        }

        void OnPlayerLogin(Player* player) override
        {
            Template const* t = TemplateOf(player);
            if (t)
                ApplyProficiencies(player, *t);
        }

        void OnPlayerFirstLogin(Player* player) override
        {
            Template const* t = TemplateOf(player);
            if (t)
                GrantStartingKit(player, *t);
        }

        void OnPlayerOnline(Player* player) override
        {
            Template const* t = TemplateOf(player);
            if (!t)
                return;
            player->SetUnboundClassMask(player->GetUnboundClassMask() | t->unboundMaskBit);
        }

        void OnPlayerAfterUpdateMaxHealth(Player* player, float& value) override
        {
            Template const* t = TemplateOf(player);
            if (!t)
                return;
            uint32 const target = t->health.ValueAt(player->GetLevel());
            if (target == 0)
                return;
            // Replace the class base (create health + stamina bonus) with the
            // template curve, applying the class-level BASE_PCT multiplier like
            // the vanilla path does. Items/auras (TOTAL_*) are untouched.
            float const replacement = float(target) -
                float(player->GetCreateHealth()) -
                player->GetHealthBonusFromStamina();
            value += replacement * player->GetPctModifierValue(UNIT_MOD_HEALTH, UnitModifierPctType::BASE_PCT);
        }

        void OnPlayerAfterUpdateMaxPower(Player* player, Powers& power, float& value) override
        {
            if (power != POWER_MANA)
                return;
            Template const* t = TemplateOf(player);
            if (!t)
                return;
            uint32 const target = t->mana.ValueAt(player->GetLevel());
            if (target == 0)
                return;
            UnitMods const unitMod = UnitMods(UNIT_MOD_POWER_START + power);
            float const bonusPower = player->GetCreatePowers(power) > 0 ?
                player->GetManaBonusFromIntellect() : 0.0f;
            float const replacement = float(target) -
                float(player->GetCreatePowers(power)) - bonusPower;
            value += replacement * player->GetPctModifierValue(unitMod, UnitModifierPctType::BASE_PCT);
        }

        void OnPlayerAfterUpdateAttackPowerAndDamage(Player* player, float& level,
            float& baseAttPower, float& /*attPowerMod*/, float& /*attPowerMultiplier*/, bool ranged) override
        {
            Template const* t = TemplateOf(player);
            if (!t)
                return;
            uint32 const target = t->ap.ValueAt(ranged, static_cast<uint8>(level));
            if (target != 0)
                baseAttPower = float(target);
        }

        void OnPlayerAfterUpdateAllStats(Player* player) override
        {
            Template const* t = TemplateOf(player);
            if (!t)
                return;
            uint8 const level = player->GetLevel();

            float const spellPower = t->spellpower.ValueAt(level);
            if (spellPower > 0.0f)
            {
                int32 const current = int32(player->GetBaseSpellPowerBonus());
                int32 const delta = int32(spellPower + 0.5f) - current;
                if (delta != 0)
                    player->ApplySpellPowerBonus(delta, true);
            }

            float const crit = t->crit.ValueAt(level);
            if (crit > 0.0f)
            {
                player->SetBaseModPctValue(BaseModGroup::CRIT_PERCENTAGE, crit);
                player->SetBaseModPctValue(BaseModGroup::OFFHAND_CRIT_PERCENTAGE, crit);
                player->SetBaseModPctValue(BaseModGroup::RANGED_CRIT_PERCENTAGE, crit);
                player->UpdateCritPercentage(WeaponAttackType::BASE_ATTACK);
                player->UpdateCritPercentage(WeaponAttackType::OFF_ATTACK);
                player->UpdateCritPercentage(WeaponAttackType::RANGED_ATTACK);
            }
        }

        std::optional<bool> OnPlayerIsClass(Player const* player, Classes unitClass,
            ClassContext context) override
        {
            if (unitClass != CLASS_HUNTER || context != CLASS_CONTEXT_PET)
                return std::nullopt;
            Template const* t = TemplateOf(player);
            return (t && t->petFamily != 0) ? std::optional<bool>(true) : std::nullopt;
        }

        void OnPlayerLfgSetRoles(Player* player, uint8& roles) override
        {
            Template const* t = TemplateOf(player);
            if (!t || t->specs.empty())
                return;
            uint8 allowed = 0;
            for (auto const& spec : t->specs)
                allowed |= spec.declaredRoleMask();
            if (allowed == 0)
                return;
            uint8 const lfgMask = lfg::PLAYER_ROLE_TANK | lfg::PLAYER_ROLE_HEALER | lfg::PLAYER_ROLE_DAMAGE;
            roles = (roles & ~lfgMask) | (roles & allowed);
        }

        bool OnPlayerCanLearnTalent(Player* player, TalentEntry const* entry, uint32 rank) override
        {
            Template const* t = TemplateOf(player);
            if (!t || t->specs.empty() || !entry)
                return true;
            if (!SpellAllowed(*t, entry->TalentID))
                return false;
            if (rank < MAX_TALENT_RANK && entry->RankID[rank] != 0 &&
                !SpellAllowed(*t, entry->RankID[rank]))
                return false;
            return true;
        }

        bool OnPlayerCanLearnSpell(Player* player, uint32 spellId) override
        {
            Template const* t = TemplateOf(player);
            if (!t || t->specs.empty())
                return true;
            return SpellAllowed(*t, spellId);
        }

        void OnPlayerGetTrainerSpellState(Player const* player, uint32 /*trainerId*/,
            uint32 spellId, Trainer::SpellState& state) override
        {
            if (state != Trainer::SpellState::Available)
                return;
            Template const* t = TemplateOf(player);
            if (!t || t->specs.empty())
                return;
            if (!SpellAllowed(*t, spellId))
                state = Trainer::SpellState::Unavailable;
        }
    };

    // ------------------------------------------------------------------
    // Registration
    // ------------------------------------------------------------------

    static Registry* g_registry = nullptr;

    Registry* GetRegistryForHooks()
    {
        return g_registry;
    }

    void AddCustomClassesScripts()
    {
        g_registry = new Registry();
        new CustomClassPlayerScript();
    }

    void Addmod_custom_classesScripts()
    {
        AddCustomClassesScripts();
    }
}
