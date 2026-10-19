// AzerothCore module loader — registers AddCustomClassesScripts() with the engine.
// The top-level modules/CMakeLists.txt calls Addmod_custom_classesScripts(),
// which this file defines by forwarding to our actual registration function.

namespace CustomClasses
{
    void AddCustomClassesScripts();
}

void Addmod_custom_classesScripts()
{
    CustomClasses::AddCustomClassesScripts();
}
