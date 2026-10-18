/*
 * mod-custom-classes
 *
 * Generic, data-driven custom class system for Wrath Unbound.
 *
 * A custom class is a database template (world DB table `custom_class_template`)
 * that re-skins an existing vanilla class: it can carry its own stat growth
 * curves, spec/talent restrictions, starting kit, weapon/armor proficiencies,
 * pet family and a dedicated bit in the Wrath Unbound class mask.
 *
 * Zero templates registered (or CustomClasses.Enabled=false) is a complete no-op:
 * every hook below returns without touching vanilla behaviour.
 *
 * Contract: docs/custom-class-template.md
 */

#ifndef MOD_CUSTOM_CLASSES_CUSTOMCLASSSYSTEM_H
#define MOD_CUSTOM_CLASSES_CUSTOMCLASSSYSTEM_H

#include "DataStores/DBCStructure.h"
#include "ScriptDefines/PlayerScript.h"
#include "ScriptDefines/WorldScript.h"
#include "SharedDefines.h"

#include <algorithm>
#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace CustomClasses
{
    // Unbound class mask allocation policy: one unique single bit per template,
    // drawn from 1<<11 .. 1<<31. Vanilla class bits are 1<<0 .. 1<<8 plus
    // 1<<10 (druid); 1<<9 is reserved; bits below 1<<11 are therefore never
    // used by the vanilla class mask or by unbound character unlocks.
    constexpr uint32 MaskBitLow = 1u << 11;
    constexpr uint32 MaskBitHigh = 1u << 31;
    constexpr int MaxPlayerLevel = 80;

    // Per-level growth curve. Values are the total base value at a level
    // (replacing the vanilla class base for that stat, not adding to it).
    struct StatCurve
    {
        void SetValue(uint8 level, uint32 value)
        {
            for (auto& entry : m_points)
                if (entry.first == level)
                {
                    entry.second = value;
                    return;
                }
            m_points.emplace_back(level, value);
        }

        uint32 ValueAt(uint8 level) const
        {
            if (m_points.empty())
                return 0;
            auto it = std::upper_bound(m_points.begin(), m_points.end(),
                std::pair<uint8, uint32>(level, UINT32_MAX),
                [](std::pair<uint8, uint32> const& a, std::pair<uint8, uint32> const& b)
                { return a.first < b.first; });
            if (it == m_points.begin())
                return m_points.front().second;
            return std::prev(it)->second;
        }

        bool empty() const { return m_points.empty(); }
        bool operator==(StatCurve const& other) const { return m_points == other.m_points; }

    private:
        std::vector<std::pair<uint8, uint32>> m_points;
    };

    // Attack power: melee curve is required; ranged is optional (defaults to melee).
    struct ApCurve
    {
        StatCurve melee;
        StatCurve ranged;
        uint32 ValueAt(bool useRanged, uint8 level) const
        {
            if (useRanged && !ranged.empty())
                return ranged.ValueAt(level);
            return melee.ValueAt(level);
        }
    };

    // LFG role bits, mirroring lfg::PLAYER_ROLE_* (Tank 0x02, Healer 0x04, Damage 0x08).
    struct SpecRole
    {
        enum Type
        {
            None = 0x00,
            Tank = 0x02,
            Healer = 0x04,
            Damage = 0x08
        };
    };

    // One spec: a vanilla talent tab this template offers, its LFG role and an
    // optional whitelist of learnable talent ids (empty = entire tree allowed).
    struct Spec
    {
        uint32 talentTree = 0;
        uint8 role = SpecRole::None;
        std::vector<uint32> spellWhitelist;

        bool specCoversTalentTab(uint32 tab) const { return talentTree == tab; }
        bool spellWhitelisted(uint32 spellId) const
        {
            if (spellWhitelist.empty())
                return true;
            return std::find(spellWhitelist.begin(), spellWhitelist.end(), spellId) != spellWhitelist.end();
        }
        uint8 declaredRoleMask() const { return role; }
    };

    struct StartingKit
    {
        std::vector<uint32> spells;
        std::vector<uint32> items;
        std::vector<uint32> actionBar; // informational: client-side action bar slots
    };

    struct Template
    {
        std::string key;
        std::string displayName;
        uint8 backingClass = 0; // vanilla ClassID (1-9, 11)
        std::string color;
        std::string icon;
        uint8 primaryStat = 0;
        StatCurve health;
        StatCurve mana;
        ApCurve ap;
        StatCurve spellpower;
        StatCurve crit;
        std::vector<Spec> specs;
        StartingKit startingKit;
        uint16 petFamily = 0;
        uint32 weaponProfMask = 0;
        uint32 armorProfMask = 0;
        uint32 unboundMaskBit = 0;
        bool enabled = true;
        std::string description;

        // Precomputed during DBC validation: the set of spells this template
        // blocks for characters of its backing class. Empty = no gating.
        std::unordered_set<uint32> blockedSpells;
    };

    class Registry : public WorldScript
    {
    public:
        Registry();

        bool IsEnabled() const { return m_enabled; }
        bool IsLoaded() const { return !m_byClass.empty(); }
        Template const* GetTemplate(uint8 backingClass) const;
        Template const* GetPlayerTemplate(Player const* player);

    private:
        void OnAfterConfigLoad(bool reload) override;
        void OnBeforeWorldInitialized() override;

        void LoadFromDatabase();
        void ValidateAgainstDBC();
        bool ValidateTemplateAgainstDBC(Template const& t,
            std::unordered_set<uint32> const& existingTalents, std::string& err) const;
        void ComputeBlockedSpells(Template& t) const;

        std::unordered_map<uint8, Template> m_byClass;
        bool m_enabled = true;
    };

    bool SpellAllowed(Template const& t, uint32 spellId);

    void AddCustomClassesScripts();
    void Addmod_custom_classesScripts();
}

#endif
