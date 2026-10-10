package org.bukkit.plugin.java;
public abstract class JavaPlugin implements org.bukkit.plugin.Plugin {
    public void onEnable() {}
    public void onDisable() {}
    public java.util.logging.Logger getLogger() { return null; }
    public void saveDefaultConfig() {}
    public org.bukkit.configuration.file.FileConfiguration getConfig() { return null; }
}
