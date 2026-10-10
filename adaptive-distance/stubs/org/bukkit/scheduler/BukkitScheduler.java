package org.bukkit.scheduler;
public interface BukkitScheduler {
    BukkitTask runTaskTimer(org.bukkit.plugin.Plugin plugin, Runnable task, long delay, long period);
}
