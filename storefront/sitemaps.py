from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from product.models import Product, ProductCategory
from storefront.models import HeroSlide


class StaticViewSitemap(Sitemap):
    priority = 0.5
    changefreq = 'daily'

    def items(self):
        return [
            'storefront:home',
            'storefront:product_list',
            'storefront:category_list',
            'storefront:about',
            'storefront:return_policy',
            'storefront:terms_of_service',
            'accounts:login',
            'accounts:register',
        ]

    def location(self, item):
        return reverse(item)


class ProductSitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.8

    def items(self):
        return Product.active_objects.filter(is_active=True)

    def lastmod(self, obj):
        return obj.updated_at


class CategorySitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.7

    def items(self):
        return ProductCategory.objects.filter(is_active=True)

    def lastmod(self, obj):
        return obj.updated_at


class HeroSlideSitemap(Sitemap):
    changefreq = 'daily'
    priority = 0.6

    def items(self):
        return HeroSlide.objects.filter(is_active=True)

    def lastmod(self, obj):
        return obj.updated_at